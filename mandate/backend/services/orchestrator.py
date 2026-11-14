"""Purchase flow: propose -> evaluate+reserve -> claim -> rail -> settle -> deliver.

Order matters. The dispatch claim is committed before the rail is called, and
settlement is committed before delivery is checked.
"""
from __future__ import annotations

from typing import Any

from ..core.ledger import Ledger, Stage
from ..core.policy import Decision, Intent
from .payment_rail import PaymentRail, RailOutcome
from .seller import Seller, verify_delivery


def _deliver(ledger: Ledger, seller: Seller, purchase_id: str) -> None:
    p = ledger.purchase(purchase_id)
    try:
        payload = seller.fetch(item=p["item"], rail_order_id=p["rail_order_id"], request_id=p["id"])
    except Exception as exc:  # seller outage after payment
        ledger.record_delivery(purchase_id, False, f"Seller unreachable ({type(exc).__name__})")
        return
    d = verify_delivery(payload, item=p["item"], request_id=p["id"])
    ledger.record_delivery(purchase_id, d.ok, d.detail)


def execute(ledger: Ledger, rail: PaymentRail, seller: Seller, purchase_id: str) -> dict[str, Any]:
    """Pay for a RESERVED purchase. Safe to call once; a second call cannot claim again."""
    claim_key = ledger.claim_dispatch(purchase_id)  # durable intent, before any money moves
    amount = ledger.purchase(purchase_id)["amount"]
    res = rail.create_order(dapp_order_id=claim_key, amount_milli=amount)

    if res.outcome is RailOutcome.SETTLED and res.order_id:
        ledger.settle(purchase_id, res.order_id)
        _deliver(ledger, seller, purchase_id)
    elif res.outcome is RailOutcome.UNPAID:
        ledger.close_unpaid(purchase_id, res.status or "rail refused the order")
    elif res.outcome is RailOutcome.PENDING and res.order_id:
        # Order exists; payment not yet confirmed. Stay DISPATCHED until reconcile() sees it settle.
        ledger.attach_order(purchase_id, res.order_id)
    else:
        ledger.mark_unknown(purchase_id, "Rail gave no trustworthy answer")
    return ledger.purchase(purchase_id)


def reconcile(ledger: Ledger, rail: PaymentRail, seller: Seller, purchase_id: str,
              rail_order_id: str | None = None) -> dict[str, Any]:
    """Resolve an open purchase by asking the rail about the ORIGINAL order. Never re-creates one."""
    p = ledger.purchase(purchase_id)
    if p["stage"] == Stage.RECOVERY_UNDELIVERED.value:
        _deliver(ledger, seller, purchase_id)
        return ledger.purchase(purchase_id)
    if p["stage"] not in (Stage.RECOVERY_UNKNOWN.value, Stage.DISPATCHED.value):
        return p
    order_id = rail_order_id or p["rail_order_id"]
    if not order_id:
        return p  # no reference to look up: stays in recovery for an operator
    res = rail.get_order(order_id)
    if res.outcome is RailOutcome.SETTLED:
        ledger.settle(purchase_id, order_id)
        _deliver(ledger, seller, purchase_id)
    elif res.outcome is RailOutcome.UNPAID:
        ledger.close_unpaid(purchase_id, res.status or "unpaid")
    return ledger.purchase(purchase_id)


def submit(ledger: Ledger, rail: PaymentRail, seller: Seller, *, request_key: str, intent: Intent,
           tool_call: dict[str, Any], session_key_id: str | None = None) -> dict[str, Any]:
    """Agent entry point. Idempotent on request_key."""
    purchase, created = ledger.propose(request_key=request_key, intent=intent, tool_call=tool_call,
                                       session_key_id=session_key_id)
    if not created:
        return purchase
    ev = ledger.evaluate_and_reserve(purchase["id"])
    if ev.decision is Decision.AUTO_APPROVED:
        return execute(ledger, rail, seller, purchase["id"])
    return ledger.purchase(purchase["id"])
