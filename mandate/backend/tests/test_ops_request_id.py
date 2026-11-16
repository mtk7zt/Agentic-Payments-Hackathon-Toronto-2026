"""Every response carries X-Request-ID: the caller's when it is well formed, a fresh UUID4 otherwise."""

import secrets
import uuid

import pytest
from fastapi.testclient import TestClient

from backend.api import ops
from backend.api.main import app

OWNER_TOKEN = secrets.token_urlsafe(32)
c = TestClient(app, headers={"X-Owner-Token": OWNER_TOKEN})


@pytest.fixture(autouse=True)
def configured_owner(monkeypatch):
    monkeypatch.setenv("MANDATE_OWNER_TOKEN", OWNER_TOKEN)


def is_uuid4(value: str) -> bool:
    try:
        return uuid.UUID(value).version == 4
    except ValueError:
        return False


def test_valid_request_id_is_echoed():
    r = c.get("/health", headers={"X-Request-ID": "req-ABC-12345"})
    assert r.status_code == 200 and r.headers["X-Request-ID"] == "req-ABC-12345"


def test_missing_request_id_gets_a_uuid4():
    assert is_uuid4(c.get("/health").headers["X-Request-ID"])


@pytest.mark.parametrize("bad", ["short", "x" * 65, "has space 123", "semi;colon-1", "under_score-1", ""])
def test_malformed_request_id_is_replaced(bad):
    rid = c.get("/health", headers={"X-Request-ID": bad}).headers["X-Request-ID"]
    assert rid != bad and is_uuid4(rid)


def test_request_id_is_fresh_per_request():
    assert c.get("/health").headers["X-Request-ID"] != c.get("/health").headers["X-Request-ID"]


def test_error_responses_carry_the_id_too():
    assert is_uuid4(c.get("/no/such/route").headers["X-Request-ID"])
    assert is_uuid4(c.get("/api/v1/state", headers={"X-Owner-Token": "wrong"}).headers["X-Request-ID"])
    r = c.get("/api/v1/state", headers={"X-Request-ID": "owner-read-0001"})
    assert r.status_code == 200 and r.headers["X-Request-ID"] == "owner-read-0001"


def test_request_id_helper_matches_the_contract():
    assert ops.request_id_from("abcdefgh") == "abcdefgh"
    assert ops.request_id_from("A-" * 32) == "A-" * 32
    assert is_uuid4(ops.request_id_from(None))
    assert is_uuid4(ops.request_id_from("abc"))
    assert is_uuid4(ops.request_id_from("A-" * 33))
