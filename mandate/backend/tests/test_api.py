from fastapi.testclient import TestClient

from backend.api.main import app

c = TestClient(app)


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
    assert c.post(f"/api/v1/purchases/{held['id']}/approve").json()["stage"] == "VERIFIED"
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
