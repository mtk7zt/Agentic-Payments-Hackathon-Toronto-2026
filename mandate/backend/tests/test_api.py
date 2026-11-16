import secrets

import pytest
from fastapi.testclient import TestClient

from backend.api.main import app

OWNER_TOKEN = secrets.token_urlsafe(32)
c = TestClient(app, headers={"X-Owner-Token": OWNER_TOKEN})


@pytest.fixture(autouse=True)
def configured_owner(monkeypatch):
    monkeypatch.setenv("MANDATE_OWNER_TOKEN", OWNER_TOKEN)


def key():
    return c.post("/api/v1/session-keys").json()["token"]


def body(n, dom="seller-a.example", addr="0x00000000000000000000000000000000000000a1", amount="0.040"):
    return {"request_key": f"api-test-{n:04d}", "seller_domain": dom, "seller_address": addr,
            "item": "shipping-estimate", "amount": amount, "tool_call": {"tool": "buy_supplier_data"}}


def test_agent_needs_a_live_session_key():
    assert c.post("/api/v1/purchases", json=body(1)).status_code == 401


def test_flow_through_the_api():
    k = {"X-Session-Key": key()}
    assert c.post("/api/v1/purchases", json=body(2), headers=k).json()["stage"] == "VERIFIED"
    held = c.post("/api/v1/purchases", json=body(3, "seller-b.example", "0x00000000000000000000000000000000000000b2", "2.500"), headers=k).json()
    assert held["stage"] == "PENDING_APPROVAL"
    summary = c.get(f"/api/v1/purchases/{held['id']}/approval").json()
    assert summary["seller_domain"] == "seller-b.example" and summary["amount"] == "2.500"
    assert c.post(f"/api/v1/purchases/{held['id']}/approve").status_code == 422  # digest required
    assert c.post(f"/api/v1/purchases/{held['id']}/approve", json={"digest": "0" * 64}).status_code == 409
    assert c.post(f"/api/v1/purchases/{held['id']}/approve", json={"digest": summary["digest"]}).json()["stage"] == "VERIFIED"
    assert c.post("/api/v1/purchases", json={**body(4), "amount": 0.04}, headers=k).status_code == 422
    assert c.post("/api/v1/purchases", json={**body(5), "extra": 1}, headers=k).status_code == 422
    blocked = c.post("/api/v1/purchases", json=body(6, "attacker.eth", "0xdead", "2.500"), headers=k).json()
    assert blocked["stage"] == "BLOCKED" and blocked["clause"] == "VENDOR_NOT_WHITELISTED"
    assert c.post("/api/v1/purchases", json=body(7), headers=k).status_code == 401  # that key is now revoked
    s = c.get("/api/v1/state").json()
    assert s["balances"]["settled"] == 2540 and s["chain"]["intact"]


def test_kill_switch_returns_a_revocation_slip():
    token = key()
    r = c.post("/api/v1/kill-switch", json={"engaged": True}).json()
    assert r["engaged"] and r["revoked"] and r["slip"][0]["type"] == "SESSION_KEY_REVOKED" and r["chain"]["intact"]
    assert c.post("/api/v1/purchases", json=body(8), headers={"X-Session-Key": token}).status_code == 401
    assert c.post("/api/v1/kill-switch", json={"engaged": False}).json() == {"engaged": False}


def test_state_lists_session_keys_without_tokens():
    issued = c.post("/api/v1/session-keys").json()
    keys = c.get("/api/v1/state").json()["session_keys"]
    assert any(k["key_id"] == issued["key_id"] and k["state"] == "active" for k in keys)
    assert all("token" not in k and issued["token"] not in str(k) for k in keys)


def test_event_timestamps_are_integer_milliseconds():
    # The browser re-hashes events; an integer serializes identically in Python and JavaScript.
    from backend.api.main import ledger

    assert all(isinstance(e["ts"], int) and e["ts"] > 1_600_000_000_000 for e in ledger.events())
    assert ledger.verify_chain()["intact"]


def test_api_events_route_is_registered():
    paths = {r.path for r in app.routes}
    assert {"/api/events", "/api/v1/stream"} <= paths
