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
    except Exception as exc:  # noqa: BLE001 - any seller failure after payment must retain settlement
        ledger.record_delivery(purchase_id, False, f"Seller unreachable ({type(exc).__name__})")
        return
    d = verify_delivery(payload, item=p["item"], request_id=p["id"], rail_order_id=p["rail_order_id"])
    ledger.record_delivery(purchase_id, d.ok, d.detail)


def deliver(ledger: Ledger, seller: Seller, purchase_id: str) -> dict[str, Any]:
    """Fetch and verify delivery for a purchase that is already settled."""
    _deliver(ledger, seller, purchase_id)
    return ledger.purchase(purchase_id)


def execute(ledger: Ledger, rail: PaymentRail, seller: Seller, purchase_id: str) -> dict[str, Any]:
    """Pay for a RESERVED purchase. Safe to call once; a second call cannot claim again."""
    claim_key = ledger.claim_dispatch(purchase_id, getattr(rail, "name", None))  # durable intent, before any money moves
    p0 = ledger.purchase(purchase_id)
    amount = p0["amount"]
    try:
        res = rail.create_order(dapp_order_id=claim_key, amount_milli=amount, item=p0["item"])
    except Exception as exc:  # noqa: BLE001 - any adapter failure may follow an accepted payment
        # An adapter can fail after the rail accepted the request. Never assume
        # an exception proves the payment did not happen.
        ledger.mark_unknown(purchase_id, f"Rail call interrupted ({type(exc).__name__})")
        return ledger.purchase(purchase_id)

    if res.outcome is RailOutcome.SETTLED and res.order_id:
        ledger.settle(purchase_id, res.order_id)
        _deliver(ledger, seller, purchase_id)
    elif res.outcome is RailOutcome.UNPAID:
        ledger.close_unpaid(purchase_id, res.status or "rail refused the order")
    elif res.outcome is RailOutcome.PENDING and res.order_id:
        # Order exists; payment not yet confirmed. Stay DISPATCHED until reconcile() sees it settle.
        ledger.attach_order(purchase_id, res.order_id, (res.raw or {}).get("approval_id"))
    else:
        ledger.mark_unknown(purchase_id, "Rail gave no trustworthy answer")
    return ledger.purchase(purchase_id)


def reconcile(ledger: Ledger, rail: PaymentRail, seller: Seller, purchase_id: str,
              rail_order_id: str | None = None) -> dict[str, Any]:
    """Resolve an open purchase by asking the rail about the ORIGINAL order. Never re-creates one.

    Identity comes from the original order reference: the stored rail order id, or a lookup by the
    dapp_order_id Mandate sent at dispatch. An order id typed in by the owner is accepted only if the
    rail does not contradict it. Amount or balance alone never proves which payment is which.
    Only a definitive UNPAID answer releases the hold; anything else keeps it.
    """
    p = ledger.purchase(purchase_id)
    if p["stage"] == Stage.RECOVERY_UNDELIVERED.value:
        _deliver(ledger, seller, purchase_id)
        return ledger.purchase(purchase_id)
    if p["stage"] not in (Stage.RECOVERY_UNKNOWN.value, Stage.DISPATCHED.value):
        return p
    claim_key = ledger.claim_key(purchase_id)
    order_id = p["rail_order_id"]
    source = "stored reference"
    lookup = getattr(rail, "find_by_dapp_order_id", None)
    if not order_id and claim_key and callable(lookup):
        order_id = lookup(claim_key)
        source = "lookup by original dapp_order_id"
    if not order_id and rail_order_id:
        order_id, source = rail_order_id, "reference supplied by the owner"
    elif order_id and rail_order_id and rail_order_id != order_id:
        ledger.record_reconcile(purchase_id, f"Owner reference {rail_order_id} ignored: purchase is bound to {order_id}",
                                ref=rail_order_id)
    if not order_id:
        if p["stage"] == Stage.DISPATCHED.value:
            ledger.mark_unknown(purchase_id, "Dispatch has no durable rail order reference")
        ledger.record_reconcile(purchase_id, "No order reference and no lookup on this rail. "
                                             "Hold kept; inspect the provider and resolve by hand.")
        return ledger.purchase(purchase_id)
    res = rail.get_order(order_id)
    seen = (res.raw or {}).get("data", res.raw or {})
    seen_key = seen.get("dapp_order_id") if isinstance(seen, dict) else None
    if seen_key and claim_key and seen_key != claim_key:
        ledger.record_reconcile(purchase_id, f"Order {order_id} belongs to {seen_key}, not this purchase. Hold kept.",
                                ref=order_id)
        return ledger.purchase(purchase_id)
    if res.outcome is RailOutcome.SETTLED:
        ledger.settle(purchase_id, res.order_id or order_id)
        _deliver(ledger, seller, purchase_id)
    elif res.outcome is RailOutcome.UNPAID:
        ledger.close_unpaid(purchase_id, res.status or "unpaid")
    else:
        if p["stage"] == Stage.DISPATCHED.value and res.outcome is RailOutcome.UNKNOWN:
            ledger.mark_unknown(purchase_id, f"Rail lookup gave no answer ({res.status or 'unknown'})")
        ledger.record_reconcile(purchase_id, f"Rail says {res.outcome.value} via {source}. Hold kept.", ref=order_id)
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
