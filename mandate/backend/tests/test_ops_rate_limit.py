"""Token-bucket rate limiting per owner token hash, per agent session key hash, or per client IP."""

import secrets

import pytest
from fastapi.testclient import TestClient

from backend.api import main, ops
from backend.api.main import app

OWNER_TOKEN = secrets.token_urlsafe(32)
ORIGIN = {"Origin": "http://localhost:3000"}
c = TestClient(app, headers={"X-Owner-Token": OWNER_TOKEN})
anon = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_limiter(monkeypatch):
    monkeypatch.setenv("MANDATE_OWNER_TOKEN", OWNER_TOKEN)
    monkeypatch.setenv("MANDATE_RATE_LIMIT_ENABLED", "1")  # the suite disables it in conftest; these tests want the real thing
    main.rate_limiter.reset()
    yield
    main.rate_limiter.reset()


def session_key() -> str:
    return c.post("/api/v1/session-keys").json()["token"]


def test_defaults_are_60_for_agents_and_300_for_owners_and_on(monkeypatch):
    for name in ("MANDATE_RATE_LIMIT_AGENT_PER_MIN", "MANDATE_RATE_LIMIT_OWNER_PER_MIN", "MANDATE_RATE_LIMIT_ANON_PER_MIN",
                 "MANDATE_RATE_LIMIT_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    limits = ops.rate_limits_from_env()
    assert (limits.agent_per_min, limits.owner_per_min, limits.anon_per_min, limits.enabled) == (60, 300, 60, True)


@pytest.mark.parametrize("value,enabled", [("0", False), ("false", False), ("FALSE", False), ("1", True), ("true", True),
                                           ("off", True), ("", True)])
def test_enabled_switch_only_honours_zero_and_false(monkeypatch, value, enabled):
    monkeypatch.setenv("MANDATE_RATE_LIMIT_ENABLED", value)
    assert ops.rate_limits_from_env().enabled is enabled


def test_disabled_switch_lets_everything_through(monkeypatch):
    monkeypatch.setenv("MANDATE_RATE_LIMIT_ENABLED", "0")
    monkeypatch.setenv("MANDATE_RATE_LIMIT_OWNER_PER_MIN", "1")
    assert all(c.get("/api/v1/state").status_code == 200 for _ in range(5))
    assert main.rate_limiter.keys() == []


def test_production_warns_at_startup_when_disabled(monkeypatch):
    import io
    import json
    import logging

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(ops.JsonFormatter())
    logging.getLogger("mandate.ops").addHandler(handler)
    try:
        monkeypatch.setenv("MANDATE_RATE_LIMIT_ENABLED", "0")
        ops.check_rate_limit_config("sandbox")
        assert buf.getvalue() == ""
        ops.check_rate_limit_config("production")
        warning = json.loads(buf.getvalue().strip())
        assert warning["level"] == "WARNING" and "DISABLED" in warning["msg"]
        monkeypatch.setenv("MANDATE_RATE_LIMIT_ENABLED", "1")
        buf.truncate(0)
        buf.seek(0)
        assert ops.check_rate_limit_config("production").enabled and buf.getvalue() == ""
    finally:
        logging.getLogger("mandate.ops").removeHandler(handler)


def test_limits_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("MANDATE_RATE_LIMIT_AGENT_PER_MIN", "7")
    monkeypatch.setenv("MANDATE_RATE_LIMIT_OWNER_PER_MIN", "9")
    monkeypatch.setenv("MANDATE_RATE_LIMIT_ANON_PER_MIN", "3")
    limits = ops.rate_limits_from_env()
    assert (limits.agent_per_min, limits.owner_per_min, limits.anon_per_min) == (7, 9, 3)
    monkeypatch.setenv("MANDATE_RATE_LIMIT_AGENT_PER_MIN", "lots")
    with pytest.raises(ValueError):
        ops.rate_limits_from_env()


def test_agent_gets_429_with_retry_after_and_cors_headers(monkeypatch):
    key = {"X-Session-Key": session_key(), **ORIGIN}
    monkeypatch.setenv("MANDATE_RATE_LIMIT_AGENT_PER_MIN", "3")
    for _ in range(3):
        assert anon.get("/api/v1/catalog", headers=key).status_code == 200
    r = anon.get("/api/v1/catalog", headers=key)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1
    assert r.headers["Access-Control-Allow-Origin"] == "http://localhost:3000"
    assert r.headers["Access-Control-Allow-Credentials"] == "true"
    assert r.json()["detail"] == "rate limit exceeded"


def test_each_session_key_has_its_own_bucket(monkeypatch):
    monkeypatch.setenv("MANDATE_RATE_LIMIT_AGENT_PER_MIN", "2")
    a, b = {"X-Session-Key": session_key()}, {"X-Session-Key": session_key()}
    assert [anon.get("/api/v1/catalog", headers=a).status_code for _ in range(3)] == [200, 200, 429]
    assert [anon.get("/api/v1/catalog", headers=b).status_code for _ in range(3)] == [200, 200, 429]


def test_owner_has_a_separate_higher_bucket(monkeypatch):
    monkeypatch.setenv("MANDATE_RATE_LIMIT_AGENT_PER_MIN", "1")
    monkeypatch.setenv("MANDATE_RATE_LIMIT_OWNER_PER_MIN", "4")
    assert [c.get("/api/v1/state").status_code for _ in range(5)] == [200, 200, 200, 200, 429]


def test_owner_bucket_is_keyed_by_token_hash_not_identity(monkeypatch):
    """Two different tokens (one wrong) are two buckets, and the raw token never becomes a key."""
    monkeypatch.setenv("MANDATE_RATE_LIMIT_OWNER_PER_MIN", "2")
    wrong = {"X-Owner-Token": "not-the-owner-token-at-all"}
    assert [anon.get("/api/v1/state", headers=wrong).status_code for _ in range(3)] == [403, 403, 429]
    assert c.get("/api/v1/state").status_code == 200
    keys = main.rate_limiter.keys()
    assert all(OWNER_TOKEN not in k and "not-the-owner-token" not in k for k in keys)
    assert any(k.startswith("owner:") for k in keys)


def test_anonymous_requests_are_keyed_by_client_ip(monkeypatch):
    monkeypatch.setenv("MANDATE_RATE_LIMIT_ANON_PER_MIN", "2")
    assert [anon.get("/api/v1/state").status_code for _ in range(3)] == [401, 401, 429]
    assert any(k.startswith("ip:") for k in main.rate_limiter.keys())  # noqa: SIM118


def test_forwarded_for_is_ignored_unless_proxy_headers_are_trusted(monkeypatch):
    monkeypatch.setenv("MANDATE_RATE_LIMIT_ANON_PER_MIN", "1")
    spoof = {"X-Forwarded-For": "203.0.113.9"}
    assert anon.get("/api/v1/state", headers=spoof).status_code == 401
    assert anon.get("/api/v1/state", headers={"X-Forwarded-For": "203.0.113.10"}).status_code == 429
    main.rate_limiter.reset()
    monkeypatch.setenv("MANDATE_TRUST_PROXY_HEADERS", "1")
    assert anon.get("/api/v1/state", headers=spoof).status_code == 401
    assert anon.get("/api/v1/state", headers={"X-Forwarded-For": "203.0.113.10"}).status_code == 401
    assert anon.get("/api/v1/state", headers=spoof).status_code == 429


def test_zero_disables_a_limit(monkeypatch):
    monkeypatch.setenv("MANDATE_RATE_LIMIT_OWNER_PER_MIN", "0")
    assert all(c.get("/api/v1/state").status_code == 200 for _ in range(20))


def test_token_bucket_refills_over_time():
    now = [1000.0]
    bucket = ops.InMemoryTokenBucket(clock=lambda: now[0])
    assert all(bucket.acquire("k", limit=2, window_s=60).allowed for _ in range(2))
    denied = bucket.acquire("k", limit=2, window_s=60)
    assert not denied.allowed and denied.retry_after == 30  # one token per 30s at 2/min
    now[0] += 29
    assert not bucket.acquire("k", limit=2, window_s=60).allowed
    now[0] += 1
    assert bucket.acquire("k", limit=2, window_s=60).allowed
    now[0] += 3600
    assert sum(bucket.acquire("k", limit=2, window_s=60).allowed for _ in range(5)) == 2  # capacity never exceeds the limit


def test_token_bucket_forgets_idle_keys():
    now = [0.0]
    bucket = ops.InMemoryTokenBucket(clock=lambda: now[0], max_keys=3)
    for i in range(3):
        bucket.acquire(f"k{i}", limit=5)
    now[0] += 120
    bucket.acquire("k-new", limit=5)
    assert set(bucket.keys()) == {"k-new"}
