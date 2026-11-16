"""MND-06: persisted request envelopes and separate owner/agent capabilities."""

import hashlib
import secrets

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.agent.runner import AgentGateway, AgentRunner
from backend.api import main as gateway
from backend.core.ledger import Ledger, LedgerError, canonical
from backend.core.policy import Intent, Policy
from backend.services import orchestrator
from backend.services.payment_rail import MockRail
from backend.services.seller import MockSeller

SELLER = ("seller-a.example", "0xa1")
CALL = {"tool": "buy_supplier_data", "arguments": {"route": "Toronto", "units": 1}}


def test_regression_gateway_starts_with_mock_integrations():
    assert gateway.rail.name == "mock" and isinstance(gateway.seller, MockSeller)


def test_changed_tool_arguments_do_not_reuse_paid_intent(tmp_path):
    path = str(tmp_path / "intents.sqlite3")
    ledger = Ledger(path, policy=Policy(whitelisted_sellers=dict([SELLER])))
    rail = MockRail()
    original = orchestrator.submit(
        ledger, rail, MockSeller(), request_key="immutable-request",
        intent=Intent(*SELLER, "shipping-estimate", 40), tool_call=CALL,
    )
    ledger.conn.close()
    ledger = Ledger(path)
    try:
        before = (ledger.balances(), ledger.events())
        with pytest.raises(LedgerError, match="different purchase"):
            ledger.propose(
                request_key="immutable-request", intent=Intent(*SELLER, "shipping-estimate", 40),
                tool_call={"tool": "buy_supplier_data", "arguments": {"route": "Ottawa", "units": 1}},
            )
        replay, created = ledger.propose(
            request_key="immutable-request", intent=Intent(*SELLER, "shipping-estimate", 40),
            tool_call={"arguments": {"units": 1, "route": "Toronto"}, "tool": "buy_supplier_data"},
        )
        assert not created and replay["id"] == original["id"]
        assert (ledger.balances(), ledger.events()) == before and rail.create_calls == 1
    finally:
        ledger.conn.close()


def test_previously_stored_fingerprint_can_replay_without_rewriting_claim(tmp_path):
    path = str(tmp_path / "legacy.sqlite3")
    ledger = Ledger(path, policy=Policy(whitelisted_sellers=dict([SELLER])))
    purchase, _ = ledger.propose(
        request_key="legacy-request", intent=Intent(*SELLER, "shipping-estimate", 40), tool_call=CALL,
    )
    legacy = hashlib.sha256(canonical({
        "seller_domain": SELLER[0], "seller_address": SELLER[1], "item": "shipping-estimate", "amount": 40,
    }).encode()).hexdigest()
    ledger.conn.execute("UPDATE purchases SET fingerprint=? WHERE id=?", (legacy, purchase["id"]))
    ledger.evaluate_and_reserve(purchase["id"])
    claim = ledger.claim_dispatch(purchase["id"])
    ledger.conn.close()
    ledger = Ledger(path)
    try:
        replay, created = ledger.propose(
            request_key="legacy-request", intent=Intent(*SELLER, "shipping-estimate", 40), tool_call=CALL,
        )
        assert not created and replay["fingerprint"] == legacy
        assert claim == f"mnd-{legacy[:8]}-{purchase['id']}"
        with pytest.raises(LedgerError):
            ledger.propose(
                request_key="legacy-request", intent=Intent(*SELLER, "shipping-estimate", 40), tool_call={},
            )
        assert ledger.balances()["held"] == 40 and ledger.verify_chain()["intact"]
    finally:
        ledger.conn.close()


@pytest.fixture
def api(monkeypatch):
    ledger = Ledger(":memory:", policy=Policy(whitelisted_sellers=dict([SELLER])))
    owner_token = secrets.token_urlsafe(32)
    _, session = ledger.issue_session_key()
    rail = MockRail()
    monkeypatch.setenv("MANDATE_OWNER_TOKEN", owner_token)
    monkeypatch.setattr(gateway, "ledger", ledger)
    monkeypatch.setattr(gateway, "rail", rail)
    monkeypatch.setattr(gateway, "seller", MockSeller())
    with TestClient(gateway.app) as client:
        yield client, ledger, rail, session, owner_token
    ledger.conn.close()


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/api/v1/session-keys", {}), ("POST", "/api/v1/kill-switch", {"engaged": True}),
    ("PATCH", "/api/v1/policy", {"max_single_spend": "2.000"}),
    ("POST", "/api/v1/purchases/pur_0001/approve", {}),
    ("POST", "/api/v1/purchases/pur_0001/reject", {}),
    ("POST", "/api/v1/purchases/pur_0001/reconcile", {}),
    ("GET", "/api/v1/state", None), ("GET", "/api/v1/evidence/verify", None),
    ("GET", "/api/v1/purchases/pur_0001/evidence", None), ("GET", "/api/v1/stream", None),
])
def test_agent_cannot_call_owner_routes_when_owner_config_is_missing(api, monkeypatch, method, path, body):
    client, ledger, _, session, _ = api
    monkeypatch.delenv("MANDATE_OWNER_TOKEN")
    before = ledger.events()
    response = client.request(method, path, json=body, headers={"X-Session-Key": session})
    assert response.status_code == 401
    assert ledger.events() == before


def test_discovery_proposal_and_status_require_active_session(api):
    client, ledger, rail, session, owner_token = api
    assert client.get("/api/v1/catalog").status_code == 401
    discovery = client.get("/api/v1/catalog", headers={"X-Session-Key": session})
    assert discovery.status_code == 200
    assert discovery.json()["rail"] == "mock"
    assert len(discovery.json()["items"]) == 2
    assert owner_token not in discovery.text and session not in discovery.text
    body = {
        "request_key": "agent-api-request", "seller_domain": SELLER[0], "seller_address": SELLER[1],
        "item": "shipping-estimate", "amount": "0.040", "tool_call": CALL,
    }
    purchase = client.post("/api/v1/purchases", json=body, headers={"X-Session-Key": session}).json()
    assert purchase["stage"] == "VERIFIED" and rail.create_calls == 1
    url = f"/api/v1/purchases/{purchase['id']}"
    assert client.get(url).status_code == 401
    assert client.get(url, headers={"X-Session-Key": session}).json()["amount"] == "0.040"
    conflict = body | {"tool_call": {"tool": "buy_supplier_data", "arguments": {"route": "other"}}}
    assert client.post("/api/v1/purchases", json=conflict, headers={"X-Session-Key": session}).status_code == 409
    assert rail.create_calls == 1
    assert client.get("/api/v1/purchases/missing", headers={"X-Session-Key": session}).status_code == 404
    assert client.get("/api/v1/state", headers={"X-Owner-Token": owner_token}).status_code == 200
    assert ledger.verify_chain()["intact"]


def test_tool_payload_must_be_json_before_ledger_writes():
    ledger = Ledger(":memory:")
    try:
        before = ledger.events()
        with pytest.raises(LedgerError, match="tool_call"):
            ledger.propose(request_key="invalid-json", intent=Intent(*SELLER, "item", 40), tool_call={"bad": {1, 2}})
        assert ledger.events() == before and ledger.purchases() == []
    finally:
        ledger.conn.close()


def test_bounded_buyer_runs_discovery_purchase_and_status_end_to_end(api):
    client, ledger, rail, session, owner_token = api

    def transport(request):
        response = client.request(
            request.method, request.url.path, content=request.content,
            headers={"X-Session-Key": request.headers["X-Session-Key"], "Content-Type": "application/json"},
        )
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)

    def model(history, remaining):
        assert remaining > 0
        if not history:
            return {"tool": "discover"}
        if len(history) == 1:
            return {"tool": "propose", "arguments": {
                "request_key": "bounded-integration", "seller_domain": SELLER[0], "seller_address": SELLER[1],
                "item": "shipping-estimate", "amount": "0.040", "tool_call": CALL,
            }}
        if len(history) == 2:
            return {"tool": "status", "arguments": {"purchase_id": history[-1]["result"]["id"]}}
        return None

    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        result = AgentRunner(AgentGateway("http://gateway.test", session, http)).run(model)
    assert result.reason == "finished" and result.steps == 4 and result.new_attempts == 1
    assert result.observations[-1]["result"]["stage"] == "VERIFIED"
    assert rail.create_calls == 1 and ledger.balances()["settled"] == 40
    assert session not in str(result.observations) and owner_token not in str(result.observations)
    assert ledger.verify_chain()["intact"]
