"""Cross-issue authority/accounting checks for MND-06 through MND-09."""

import pytest
from fastapi.testclient import TestClient

from backend.api import main as gateway
from backend.core.ledger import Ledger, LedgerError
from backend.core.policy import Intent, Policy
from backend.services import orchestrator
from backend.services.payment_rail import MockRail
from backend.services.seller import MockSeller

SELLER = ("seller.example", "0xa1")
TOOL = {"tool": "buy_supplier_data", "arguments": {"route": "Toronto"}}


def policy():
    return Policy(
        task_budget_cap=100, whitelisted_sellers=dict([SELLER]), expires_at=100,
        argument_constraints={"route": "Toronto"},
    )


def test_both_policy_and_wallet_guards_survive_integration(monkeypatch):
    now = [90.0]  # Preserve time.time()'s float timestamp contract in the fake clock.
    monkeypatch.setattr("backend.core.ledger.time.time", lambda: now[0])
    ledger = Ledger(":memory:", policy=policy())
    rail = MockRail()
    try:
        ledger.configure_wallet(100, "owner: initial test snapshot")
        purchase, _ = ledger.propose(
            request_key="combined-guards", intent=Intent(*SELLER, "item", 40), tool_call=TOOL,
        )
        ledger.evaluate_and_reserve(purchase["id"])
        ledger.record_wallet_observation(99, "owner: mismatch test snapshot")
        with pytest.raises(LedgerError, match="wallet"):
            orchestrator.execute(ledger, rail, MockSeller(), purchase["id"])
        ledger.record_wallet_observation(100, "owner: corrected test snapshot")
        now[0] = 101.0
        with pytest.raises(LedgerError, match="POLICY_EXPIRED"):
            orchestrator.execute(ledger, rail, MockSeller(), purchase["id"])
        ledger.set_policy(expires_at=200, argument_constraints={"route": "Ottawa"})
        with pytest.raises(LedgerError, match="ARGUMENT_CONSTRAINT_VIOLATION"):
            orchestrator.execute(ledger, rail, MockSeller(), purchase["id"])
        assert rail.create_calls == 0 and ledger.balances()["held"] == 40
        assert ledger.conn.execute("SELECT COUNT(*) FROM dispatch_claims").fetchone()[0] == 0
        ledger.set_policy(argument_constraints={"route": "Toronto"})
        paid = orchestrator.execute(ledger, rail, MockSeller(), purchase["id"])
        assert paid["stage"] == "VERIFIED" and rail.create_calls == 1
        assert ledger.balances() == {"budget": 100, "held": 0, "settled": 40, "available": 60}
        assert ledger.wallet_status()["expected_milli"] == 60
        assert ledger.verify_chain()["intact"]
    finally:
        ledger.conn.close()


def test_expiry_and_wallet_mismatch_do_not_prevent_recording_original_payment(monkeypatch):
    now = [90.0]
    monkeypatch.setattr("backend.core.ledger.time.time", lambda: now[0])

    class AcceptedThenInterrupted(MockRail):
        def create_order(self, **kwargs):
            super().create_order(**kwargs)
            raise ConnectionError("response lost after synthetic acceptance")

    ledger = Ledger(":memory:", policy=policy())
    rail = AcceptedThenInterrupted()
    try:
        ledger.configure_wallet(100, "owner: initial test snapshot")
        purchase = orchestrator.submit(
            ledger, rail, MockSeller(), request_key="combined-unknown",
            intent=Intent(*SELLER, "item", 40), tool_call=TOOL,
        )
        assert purchase["stage"] == "RECOVERY_UNKNOWN" and ledger.balances()["held"] == 40
        now[0] = 101.0
        ledger.record_wallet_observation(60, "owner: debit before callback")
        assert ledger.wallet_status()["dispatch_blocked"] is True
        with pytest.raises(LedgerError, match="unresolved"):
            ledger.reconcile_wallet(60, "owner: unsafe baseline reset")
        original_order = rail.find_by_dapp_order_id(next(iter(rail.orders)))
        recovered = orchestrator.reconcile(ledger, rail, MockSeller(), purchase["id"], original_order)
        assert recovered["stage"] == "VERIFIED" and rail.create_calls == 1
        ledger.record_wallet_observation(60, "owner: original debit confirmed")
        assert ledger.wallet_status()["dispatch_blocked"] is False
        assert ledger.balances() == {"budget": 100, "held": 0, "settled": 40, "available": 60}
        assert ledger.verify_chain()["intact"]
    finally:
        ledger.conn.close()


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/v1/wallet"), ("POST", "/api/v1/wallet/configure"),
    ("POST", "/api/v1/wallet/observations"), ("POST", "/api/v1/wallet/reconcile"),
])
def test_wallet_routes_inherit_fail_closed_owner_auth(monkeypatch, method, path):
    monkeypatch.delenv("MANDATE_OWNER_TOKEN", raising=False)
    with TestClient(gateway.app) as client:
        response = client.request(method, path, json={"balance_milli": 100, "reference": "test snapshot"})
    assert response.status_code == 401
