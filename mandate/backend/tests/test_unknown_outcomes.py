"""MND-11: when Mandate cannot prove what happened, the hold stays until the original order answers."""
import pytest
from fastapi.testclient import TestClient

from backend.api import main
from backend.core.ledger import Ledger, LedgerError, Stage
from backend.core.policy import Intent, Policy
from backend.services import orchestrator
from backend.services.payment_rail import MockRail, RailOutcome, RailResult
from backend.services.seller import MockSeller

POLICY = Policy(whitelisted_sellers={"seller.example": "0xa1"}, task_budget_cap=1000)


def unknown_purchase(ledger: Ledger, rail, key="timeout") -> str:
    """A purchase whose rail call timed out after the claim."""
    out = orchestrator.submit(ledger, rail, MockSeller(), request_key=key,
                              intent=Intent("seller.example", "0xa1", "item", 40), tool_call={})
    assert out["stage"] == Stage.RECOVERY_UNKNOWN.value
    return out["id"]


class Scripted(MockRail):
    """A rail with no dapp_order_id lookup whose answers we script per order id."""

    def __init__(self, answers):
        super().__init__({"mnd-": RailOutcome.UNKNOWN})
        self.answers, self.looked_up = answers, []

    find_by_dapp_order_id = None  # this rail cannot look orders up by Mandate's reference

    def get_order(self, order_id):
        self.looked_up.append(order_id)
        return self.answers.get(order_id, RailResult(RailOutcome.UNKNOWN, order_id))


def test_timeout_keeps_hold_and_a_repeat_request_does_not_pay_twice():
    ledger, rail = Ledger(policy=POLICY), MockRail({"mnd-": RailOutcome.UNKNOWN})
    pid = unknown_purchase(ledger, rail)
    assert ledger.balances()["held"] == 40
    again = orchestrator.submit(ledger, rail, MockSeller(), request_key="timeout",
                                intent=Intent("seller.example", "0xa1", "item", 40), tool_call={})
    assert again["id"] == pid and rail.create_calls == 1  # same request: same purchase, no second order


def test_lookup_by_original_dapp_order_id_settles_once():
    ledger, rail = Ledger(policy=POLICY), MockRail({"mnd-": RailOutcome.UNKNOWN})
    pid = unknown_purchase(ledger, rail)
    out = orchestrator.reconcile(ledger, rail, MockSeller(), pid)
    (order,) = [o["order_id"] for o in rail.orders.values()]
    assert out["stage"] == Stage.VERIFIED.value and out["rail_order_id"] == order
    assert ledger.balances()["held"] == 0 and ledger.balances()["settled"] == 40


def test_without_a_lookup_the_hold_stays_and_the_attempt_is_recorded():
    ledger = Ledger(policy=POLICY)
    rail = Scripted({})
    pid = unknown_purchase(ledger, rail)
    out = orchestrator.reconcile(ledger, Scripted({}), MockSeller(), pid)
    assert out["stage"] == Stage.RECOVERY_UNKNOWN.value and ledger.balances()["held"] == 40
    assert any(e["type"] == "RECONCILE_ATTEMPTED" for e in ledger.events())


def test_order_belonging_to_another_purchase_is_refused():
    ledger = Ledger(policy=POLICY)
    rail = Scripted({"ord_x": RailResult(RailOutcome.SETTLED, "ord_x", "PAYMENT_CONFIRMED",
                                         {"data": {"dapp_order_id": "mnd-someone-else"}})})
    pid = unknown_purchase(ledger, rail)
    out = orchestrator.reconcile(ledger, rail, MockSeller(), pid, rail_order_id="ord_x")
    assert out["stage"] == Stage.RECOVERY_UNKNOWN.value and ledger.balances()["settled"] == 0


def test_one_rail_payment_cannot_settle_two_purchases():
    ledger, rail = Ledger(policy=POLICY), MockRail({"mnd-": RailOutcome.UNKNOWN})
    a = unknown_purchase(ledger, rail, "timeout-a")
    b = unknown_purchase(ledger, rail, "timeout-b")
    ledger.resolve_manually(a, outcome="PAID", reference="tx_123")
    with pytest.raises(LedgerError, match="already settled purchase"):
        ledger.resolve_manually(b, outcome="PAID", reference="tx_123")
    assert ledger.purchase(b)["stage"] == Stage.RECOVERY_UNKNOWN.value


def test_still_pending_or_unknown_never_releases():
    ledger = Ledger(policy=POLICY)
    for outcome in (RailOutcome.PENDING, RailOutcome.UNKNOWN):
        rail = Scripted({"ord_1": RailResult(outcome, "ord_1")})
        pid = unknown_purchase(ledger, rail, f"timeout-{outcome.value}")
        orchestrator.reconcile(ledger, rail, MockSeller(), pid, rail_order_id="ord_1")
        assert ledger.purchase(pid)["stage"] == Stage.RECOVERY_UNKNOWN.value
    assert ledger.balances()["held"] == 80


def test_definitive_unpaid_from_the_rail_releases():
    ledger = Ledger(policy=POLICY)
    rail = Scripted({"ord_1": RailResult(RailOutcome.UNPAID, "ord_1", "EXPIRED")})
    pid = unknown_purchase(ledger, rail)
    out = orchestrator.reconcile(ledger, rail, MockSeller(), pid, rail_order_id="ord_1")
    assert out["stage"] == Stage.EXPIRED.value and ledger.balances()["held"] == 0


def test_owner_resolution_needs_a_reference_and_is_recorded():
    ledger, rail = Ledger(policy=POLICY), MockRail({"mnd-": RailOutcome.UNKNOWN})
    pid = unknown_purchase(ledger, rail)
    with pytest.raises(LedgerError, match="reference is required"):
        ledger.resolve_manually(pid, outcome="UNPAID", reference="  ")
    ledger.resolve_manually(pid, outcome="UNPAID", reference="provider dashboard: order cancelled", note="checked 14:02")
    types = [e["type"] for e in ledger.events() if e["purchase_id"] == pid]
    assert "OPERATOR_RESOLUTION" in types and types[-1] == "HOLD_RELEASED"
    assert ledger.balances()["held"] == 0 and ledger.verify_chain()["intact"]
    with pytest.raises(LedgerError, match="only an unresolved payment"):
        ledger.resolve_manually(pid, outcome="PAID", reference="late")


def test_resolve_route_paid_then_delivery_is_verified(monkeypatch):
    ledger, rail = Ledger(policy=POLICY), MockRail({"mnd-": RailOutcome.UNKNOWN})
    monkeypatch.setattr(main, "ledger", ledger)
    monkeypatch.setattr(main, "rail", rail)
    monkeypatch.setattr(main, "seller", MockSeller())
    monkeypatch.setenv("MANDATE_OWNER_TOKEN", "owner-secret")
    pid = unknown_purchase(ledger, rail)
    c, h = TestClient(main.app), {"X-Owner-Token": "owner-secret"}
    assert c.post(f"/api/v1/purchases/{pid}/resolve", headers=h, json={"outcome": "MAYBE", "reference": "x"}).status_code == 422
    (order,) = [o["order_id"] for o in rail.orders.values()]
    out = c.post(f"/api/v1/purchases/{pid}/resolve", headers=h, json={"outcome": "PAID", "reference": order}).json()
    assert out["stage"] in ("VERIFIED", "RECOVERY_UNDELIVERED") and out["rail_tx_id"] == order
