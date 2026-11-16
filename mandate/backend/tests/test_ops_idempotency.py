"""Idempotency-Key on owner POST and PATCH routes replays the stored response for 24 hours."""

import secrets

import pytest
from fastapi.testclient import TestClient

from backend.api import main, ops
from backend.api.main import app

OWNER_TOKEN = secrets.token_urlsafe(32)
c = TestClient(app, headers={"X-Owner-Token": OWNER_TOKEN})


@pytest.fixture(autouse=True)
def fresh_store(monkeypatch):
    monkeypatch.setenv("MANDATE_OWNER_TOKEN", OWNER_TOKEN)
    main.idempotency_store.clear()
    main.rate_limiter.reset()
    yield
    main.idempotency_store.clear()


def key_count() -> int:
    return len(c.get("/api/v1/session-keys").json())


def test_same_key_and_body_replays_the_stored_response():
    before = key_count()
    first = c.post("/api/v1/session-keys", headers={"Idempotency-Key": "issue-0001"})
    second = c.post("/api/v1/session-keys", headers={"Idempotency-Key": "issue-0001"})
    assert first.status_code == 201 and second.status_code == 201
    assert first.json() == second.json()
    assert "Idempotent-Replayed" not in first.headers
    assert second.headers["Idempotent-Replayed"] == "true"
    assert key_count() == before + 1  # the second call never reached the ledger


def test_same_key_with_a_different_body_is_422():
    version = c.get("/api/v1/state").json()["policy"]["version"]
    r1 = c.patch("/api/v1/policy", json={"expected_version": version, "max_attempts": 3},
                 headers={"Idempotency-Key": "policy-0001"})
    assert r1.status_code == 200
    r2 = c.patch("/api/v1/policy", json={"expected_version": version + 1, "max_attempts": 4},
                 headers={"Idempotency-Key": "policy-0001"})
    assert r2.status_code == 422 and "Idempotency-Key" in r2.json()["detail"]
    assert c.get("/api/v1/state").json()["policy"]["max_attempts"] == 3  # the second write was not applied


def test_without_the_header_behaviour_is_unchanged():
    a = c.post("/api/v1/session-keys").json()
    b = c.post("/api/v1/session-keys").json()
    assert a["key_id"] != b["key_id"] and "Idempotent-Replayed" not in c.post("/api/v1/session-keys").headers


def test_keys_are_scoped_per_route():
    before = key_count()
    c.post("/api/v1/session-keys", headers={"Idempotency-Key": "shared-0001"})
    r = c.post("/api/v1/kill-switch", json={"engaged": False}, headers={"Idempotency-Key": "shared-0001"})
    assert r.status_code == 200 and "Idempotent-Replayed" not in r.headers
    assert key_count() == before + 1


def test_agent_routes_are_not_affected():
    token = c.post("/api/v1/session-keys").json()["token"]
    agent = TestClient(app, headers={"X-Session-Key": token, "Idempotency-Key": "agent-0001"})

    def body(n):
        return {"request_key": f"idem-agent-{n:04d}", "seller_domain": "seller-a.example",
                "seller_address": "0x00000000000000000000000000000000000000a1", "item": "shipping-estimate",
                "amount": "0.040", "tool_call": {"tool": "buy_supplier_data"}}

    a = agent.post("/api/v1/purchases", json=body(1))
    b = agent.post("/api/v1/purchases", json=body(2))
    assert a.status_code == 201 and b.status_code == 201 and a.json()["id"] != b.json()["id"]
    assert "Idempotent-Replayed" not in b.headers


def test_owner_get_routes_are_not_affected():
    r = c.get("/api/v1/state", headers={"Idempotency-Key": "get-0001"})
    assert r.status_code == 200 and "Idempotent-Replayed" not in c.get("/api/v1/state", headers={"Idempotency-Key": "get-0001"}).headers


@pytest.mark.parametrize("bad", ["", "x" * 129, "has space", "tab\tkey"])
def test_malformed_keys_are_rejected(bad):
    r = c.post("/api/v1/session-keys", headers={"Idempotency-Key": bad})
    assert r.status_code == 422


def test_error_responses_are_replayed_but_server_errors_are_not(monkeypatch):
    r1 = c.post("/api/v1/kill-switch", json={"engaged": "maybe"}, headers={"Idempotency-Key": "bad-body-0001"})
    r2 = c.post("/api/v1/kill-switch", json={"engaged": "maybe"}, headers={"Idempotency-Key": "bad-body-0001"})
    assert r1.status_code == 422 and r2.status_code == 422 and r2.headers["Idempotent-Replayed"] == "true"

    calls = []
    real = main.ledger.issue_session_key

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient storage failure")
        return real()

    monkeypatch.setattr(main.ledger, "issue_session_key", flaky)
    assert c.post("/api/v1/session-keys", headers={"Idempotency-Key": "retry-0001"}).status_code == 500
    r = c.post("/api/v1/session-keys", headers={"Idempotency-Key": "retry-0001"})
    assert r.status_code == 201 and "Idempotent-Replayed" not in r.headers


def test_records_expire_after_the_ttl():
    now = [1_000_000.0]
    store = ops.InMemoryIdempotencyStore(clock=lambda: now[0])
    stored = ops.StoredResponse(status=201, headers=[(b"content-type", b"application/json")], body=b'{"a":1}')
    store.put("k", "POST /x", "h1", stored, ttl_s=24 * 3600)
    assert store.get("k", "POST /x") == ops.IdempotencyRecord("h1", stored)
    now[0] += 24 * 3600 - 1
    assert store.get("k", "POST /x") is not None
    now[0] += 2
    assert store.get("k", "POST /x") is None


def test_replay_reuses_the_stored_body_hash_contract():
    store = ops.InMemoryIdempotencyStore()
    stored = ops.StoredResponse(status=200, headers=[], body=b"{}")
    store.put("k", "PATCH /y", ops.body_sha256(b'{"n":1}'), stored, ttl_s=60)
    assert store.get("k", "PATCH /y").body_sha256 == ops.body_sha256(b'{"n":1}')
    assert store.get("k", "PATCH /y").body_sha256 != ops.body_sha256(b'{"n":2}')
    store.clear()
    assert store.get("k", "PATCH /y") is None
