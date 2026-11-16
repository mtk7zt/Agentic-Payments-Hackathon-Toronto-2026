import asyncio
import secrets

import pytest
from fastapi.testclient import TestClient

from backend.api import main as gateway
from backend.api.streams import mint_grant, read_grant, stream_frames
from backend.core.ledger import Ledger


def test_signed_grants_are_scoped_expiring_and_rotation_bound():
    secret = secrets.token_urlsafe(32)
    token = mint_grant(secret, "task", "agent", now=100)
    assert read_grant(token, secret, "task", "agent", now=101) == 160
    for value, key, task, agent, now in [
        (token + "x", secret, "task", "agent", 101),
        (token, secret, "other-task", "agent", 101),
        (token, secret, "task", "other-agent", 101),
        (token, "rotated", "task", "agent", 101),
        (token, secret, "task", "agent", 160),
        (token, "", "task", "agent", 101),
        ("bad", secret, "task", "agent", 101),
    ]:
        assert read_grant(value, key, task, agent, now=now) is None


@pytest.fixture
def api(monkeypatch):
    ledger = Ledger(":memory:")
    secret = secrets.token_urlsafe(32)
    monkeypatch.setattr(gateway, "ledger", ledger)
    monkeypatch.setenv("MANDATE_OWNER_TOKEN", secret)
    with TestClient(gateway.app) as client:
        yield client, ledger, {"X-Owner-Token": secret}
    ledger.conn.close()


def test_owner_only_grant_has_http_only_cookie_and_no_secret_response(api):
    client, ledger, owner = api
    assert client.post("/api/v1/stream-session").status_code == 401
    assert client.post("/api/v1/stream-session", headers={"X-Owner-Token": "wrong"}).status_code == 403
    response = client.post("/api/v1/stream-session", headers=owner)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Max-Age=60" in cookie
    assert response.headers["cache-control"] == "no-store"
    assert next(iter(owner.values())) not in response.text + cookie
    assert client.get("/api/v1/state").status_code == 401  # cookie is stream-only authority
    before = ledger.events()
    assert client.post("/api/v1/kill-switch", json={"engaged": True}).status_code == 401
    assert ledger.events() == before


def test_browser_cookie_authenticates_alias_and_header_cursor_wins(api, monkeypatch):
    client, _, owner = api
    seen = []

    async def finite_frames(request, ledger, cursor, valid):
        assert valid()
        seen.append(cursor)
        yield "retry: 2000\n\n"

    monkeypatch.setattr(gateway, "stream_frames", finite_frames)
    client.post("/api/v1/stream-session", headers=owner)
    for path in ("/api/events", "/api/v1/stream"):
        response = client.get(path + "?after_seq=1", headers={"Last-Event-ID": "2"})
        assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    assert seen == [2, 2]
    monkeypatch.setenv("MANDATE_OWNER_TOKEN", secrets.token_urlsafe(32))
    assert client.get("/api/events").status_code == 403


def test_https_cookie_is_secure_and_owner_unset_fails_closed(api, monkeypatch):
    client, _, owner = api
    response = client.post("https://testserver/api/v1/stream-session", headers=owner)
    assert "Secure" in response.headers["set-cookie"]
    monkeypatch.delenv("MANDATE_OWNER_TOKEN")
    assert client.post("/api/v1/stream-session", headers=owner).status_code == 403
    assert client.get("/api/v1/stream", headers=owner).status_code == 403


@pytest.mark.parametrize("path", ["/api/v1/stream", "/api/events"])
def test_stream_rejects_missing_or_invalid_grant_and_invalid_cursor(api, path):
    client, _, owner = api
    assert client.get(path).status_code == 401
    client.cookies.set("mandate_stream", "tampered")
    assert client.get(path).status_code == 403  # a credential was sent, and it is wrong
    assert client.get(path, headers={**owner, "Last-Event-ID": "garbage"}).status_code == 422
    assert client.get(path, headers={**owner, "Last-Event-ID": "-1"}).status_code == 422


class Request:
    async def is_disconnected(self):
        return False


def collect(ledger, cursor, *, valid=lambda: True, frames=3):
    async def read():
        stream = stream_frames(Request(), ledger, cursor, valid)
        output = []
        try:
            for _ in range(frames):
                try:
                    output.append(await anext(stream))
                except StopAsyncIteration:
                    break
        finally:
            await stream.aclose()
        return output
    return asyncio.run(read())


def test_replay_is_monotonic_and_read_only(api):
    _, ledger, _ = api
    ledger.issue_session_key()
    ledger.issue_session_key()
    before = (ledger.events(), ledger.balances())
    output = collect(ledger, 1, frames=3)
    assert "id: 2\n" in output[1] and "id: 3\n" in output[2]
    assert (ledger.events(), ledger.balances()) == before


def test_cursor_ahead_or_gap_requests_full_refresh(api):
    _, ledger, _ = api
    assert "event: stream.reset" in "".join(collect(ledger, 100))
    ledger.issue_session_key()
    ledger.conn.execute("DROP TRIGGER events_no_delete")
    ledger.conn.execute("DELETE FROM events WHERE seq=1")
    assert "event: stream.reset" in "".join(collect(ledger, 0))


def test_expired_open_stream_closes_without_delivering_events(api):
    _, ledger, _ = api
    output = collect(ledger, 0, valid=lambda: False)
    assert "event: session.expired" in "".join(output)
    assert "ledger.appended" not in "".join(output)
