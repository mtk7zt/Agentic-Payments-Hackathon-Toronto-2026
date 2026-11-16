"""Structured JSON logs: one line per request with fixed fields, and never a credential, query string or body."""

import io
import json
import logging
import secrets

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import ops
from backend.api.main import app

OWNER_TOKEN = secrets.token_urlsafe(32)
FIELDS = {"ts", "level", "msg", "request_id", "method", "path", "status", "duration_ms"}


@pytest.fixture(autouse=True)
def configured_owner(monkeypatch):
    monkeypatch.setenv("MANDATE_OWNER_TOKEN", OWNER_TOKEN)


@pytest.fixture
def captured():
    """Route the gateway's JSON log handler into a buffer for the test, then put the real one back."""
    buf = io.StringIO()
    ops.install_json_logging(stream=buf)
    try:
        yield buf
    finally:
        ops.install_json_logging()


def lines(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def test_every_request_logs_one_json_line_with_the_fixed_fields(captured):
    c = TestClient(app, headers={"X-Owner-Token": OWNER_TOKEN})
    r = c.get("/api/v1/state", headers={"X-Request-ID": "log-test-0001"})
    assert r.status_code == 200
    entries = [e for e in lines(captured) if e.get("request_id") == "log-test-0001"]
    assert len(entries) == 1
    e = entries[0]
    assert FIELDS <= set(e)
    assert e["level"] == "INFO" and e["method"] == "GET" and e["path"] == "/api/v1/state" and e["status"] == 200
    assert isinstance(e["duration_ms"], (int, float)) and e["duration_ms"] >= 0
    assert e["ts"].endswith("Z")


def test_logs_never_contain_tokens_query_strings_or_bodies(captured):
    c = TestClient(app, headers={"X-Owner-Token": OWNER_TOKEN})
    session_key = c.post("/api/v1/session-keys").json()["token"]
    secret_reference = "wallet-ref-" + secrets.token_hex(8)
    c.post("/api/v1/wallet/configure", json={"balance_milli": 1000, "reference": secret_reference})
    c.get("/api/v1/events", params={"limit": 7, "purchase_id": "needle-in-query"})
    c.get("/api/v1/catalog", headers={"X-Session-Key": session_key})
    c.get("/api/v1/catalog", headers={"X-Session-Key": "bogus-session-key-value"})
    raw = captured.getvalue()
    assert raw.strip()
    for secret in (OWNER_TOKEN, session_key, "bogus-session-key-value", secret_reference, "needle-in-query", "limit=7"):
        assert secret not in raw
    for e in lines(captured):
        assert "?" not in e["path"]
        assert not any(k.lower() in ("headers", "query", "body", "authorization") for k in e)


def test_error_statuses_are_logged_with_their_status(captured):
    c = TestClient(app)
    c.get("/api/v1/state", headers={"X-Request-ID": "log-test-0401"})
    e = next(e for e in lines(captured) if e["request_id"] == "log-test-0401")
    assert e["status"] == 401


def test_log_lines_emitted_inside_a_request_carry_its_request_id(captured):
    """Any `mandate.*` logger used while handling a request inherits the request id from the context."""
    inner = FastAPI()
    inner_log = logging.getLogger("mandate.test_inner")

    @inner.get("/boom")
    def boom():
        inner_log.warning("something happened mid-request")
        return {"ok": True}

    inner.add_middleware(ops.RequestContextMiddleware)
    TestClient(inner).get("/boom", headers={"X-Request-ID": "inner-req-0001"})
    entries = [e for e in lines(captured) if e["request_id"] == "inner-req-0001"]
    assert {e["msg"] for e in entries} == {"something happened mid-request", "request"}
    assert {e["level"] for e in entries} == {"WARNING", "INFO"}


def test_formatter_emits_fixed_fields_even_without_request_context():
    record = logging.LogRecord("mandate.x", logging.ERROR, __file__, 1, "standalone", None, None)
    e = json.loads(ops.JsonFormatter().format(record))
    assert FIELDS <= set(e)
    assert e["msg"] == "standalone" and e["level"] == "ERROR" and e["method"] is None and e["status"] is None


def test_log_level_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("MANDATE_LOG_LEVEL", "warning")
    try:
        assert ops.install_json_logging(stream=io.StringIO()).level == logging.WARNING
    finally:
        monkeypatch.delenv("MANDATE_LOG_LEVEL")
        assert ops.install_json_logging().level == logging.INFO
