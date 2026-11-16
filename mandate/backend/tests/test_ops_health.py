"""/live says the process is up; /ready says the ledger answers; /health is untouched."""

import secrets

import pytest
from fastapi.testclient import TestClient

from backend.api import main
from backend.api.main import app

OWNER_TOKEN = secrets.token_urlsafe(32)
c = TestClient(app)


@pytest.fixture(autouse=True)
def configured_owner(monkeypatch):
    monkeypatch.setenv("MANDATE_OWNER_TOKEN", OWNER_TOKEN)


def test_live_is_200_without_credentials():
    r = c.get("/live")
    assert r.status_code == 200 and r.json() == {"live": True}


def test_ready_is_200_when_the_ledger_reads():
    r = c.get("/ready")
    assert r.status_code == 200 and r.json() == {"ready": True}


def test_ready_is_503_when_the_ledger_read_fails(monkeypatch):
    def broken():
        raise RuntimeError("database gone")

    monkeypatch.setattr(main.ledger, "policy", broken)
    r = c.get("/ready")
    assert r.status_code == 503 and r.json() == {"ready": False}
    assert c.get("/live").status_code == 200  # liveness does not depend on storage


def test_ready_does_not_leak_the_failure_reason(monkeypatch):
    def broken():
        raise RuntimeError("postgresql://user:secretpassword@db/mandate refused")

    monkeypatch.setattr(main.ledger, "policy", broken)
    assert "secretpassword" not in c.get("/ready").text


def test_health_is_unchanged():
    r = c.get("/health")
    assert r.status_code == 200
    assert set(r.json()) == {"status", "rail", "mode"} and r.json()["status"] == "ok"


def test_probes_are_not_rate_limited(monkeypatch):
    monkeypatch.setenv("MANDATE_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("MANDATE_RATE_LIMIT_ANON_PER_MIN", "1")
    main.rate_limiter.reset()
    assert all(c.get(p).status_code == 200 for p in ("/live", "/ready", "/health", "/live", "/ready", "/health"))
