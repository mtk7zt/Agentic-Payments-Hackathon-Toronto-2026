#!/usr/bin/env python3
"""MND-27 · CLI smoke test for checkout and receipt logging.

    python scripts/smoke.py            # mock rail, no network, no money
    python scripts/smoke.py --scenarios # five mock scenarios; UNKNOWN exits 1
    python scripts/smoke.py --live     # ONE GOAT Flow Testnet3 order, 0.040 test USDC (needs GOAT_FLOW_* env).
                                       # Testnet tokens only; the script refuses any other origin or chain.

Exits non-zero if any purchase ends in an unexpected state or the evidence chain is broken.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend.core.ledger import Ledger, LedgerError
from backend.core.policy import Intent
from backend.models.money import format_milli
from backend.services import orchestrator
from backend.services.payment_rail import (
    GoatFlowConfig,
    GoatFlowRail,
    MockRail,
    RailOutcome,
)
from backend.services.seller import MockSeller

A = ("seller-a.example", "0x00000000000000000000000000000000000000a1")
B = ("seller-b.example", "0x00000000000000000000000000000000000000b2")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="create one real Testnet3 order")
    ap.add_argument("--scenarios", action="store_true", help="five mock cases; intentional UNKNOWN exits 1")
    args = ap.parse_args()
    if args.live and args.scenarios:
        ap.error("--scenarios is mock-only")

    try:
        if args.live:
            config = GoatFlowConfig.from_env()
            config.require()
            if config.api_url != "https://flow-api.testnet3.goat.network" or config.chain_id != 48816:
                raise ValueError("Testnet3 origin and chain required")
            rail = GoatFlowRail(config)
        else:
            rail = MockRail({"0005": RailOutcome.UNKNOWN} if args.scenarios else {})
    except (RuntimeError, ValueError):
        print("FAIL live configuration: supply Testnet3 credentials, origin and chain 48816", file=sys.stderr)
        return 2

    invocation = uuid.uuid4().hex if args.live else "mock"
    ledger = Ledger(":memory:", mandate_id=f"smoke-{invocation}")
    ledger.set_policy(whitelisted_sellers=dict([A, B]))
    key_id, _ = ledger.issue_session_key()
    seller = MockSeller(fail_items={"lane-rates"})

    def buy(n, dom, addr, item, amount):
        return orchestrator.submit(ledger, rail, seller, request_key=f"smoke-{invocation}-{n:04d}-key",
                                   intent=Intent(dom, addr, item, amount),
                                   tool_call={"tool": "buy_supplier_data", "vendor": dom, "item": item},
                                   session_key_id=key_id)

    if not args.scenarios:
        cases = [("micro purchase", buy(1, *A, "shipping-estimate", 40), "VERIFIED")]
    else:
        cases = [
            ("micro purchase", buy(1, *A, "shipping-estimate", 40), "VERIFIED"),
            ("above limit", buy(2, *B, "shipping-matrix-2026", 2500), "PENDING_APPROVAL"),
            ("injected redirect", buy(3, "attacker.eth", "0xdead", "shipping-matrix-2026", 2500), "BLOCKED"),
        ]
        # The attack revokes its proposing capability. Only this mock owner fixture
        # reissues a key; agent execution must never renew its own authority.
        key_id, _ = ledger.issue_session_key()
        print("MOCK owner issued a fresh session key after the blocked attack")
        cases.extend([
            ("seller fails after payment", buy(4, *B, "lane-rates", 60), "RECOVERY_UNDELIVERED"),
            ("rail timeout", buy(5, *A, "customs-codes", 80), "RECOVERY_UNKNOWN"),
        ])

    ok = True
    for name, p, want in cases:
        good = p["stage"] == want and p["stage"] != "RECOVERY_UNKNOWN"
        ok &= good
        print(f"{'PASS' if good else 'FAIL'}  {name:28} {format_milli(p['amount']):>7}  {p['stage']:22} "
              f"{p['clause'] or ''}  order={p['rail_order_id'] or '-'}")

    if args.scenarios:
        calls_before = rail.create_calls
        try:
            orchestrator.execute(ledger, rail, seller, cases[4][1]["id"])  # a blind retry must be refused
            ok = False
            print("FAIL  retry after timeout was allowed")
        except LedgerError as exc:
            print(f"PASS  retry after timeout refused       ({exc}); rail calls unchanged: {rail.create_calls == calls_before}")
            ok &= rail.create_calls == calls_before

    if args.live:  # MND-01 evidence: the real Testnet3 field names and statuses, values redacted
        def shape(body):
            if isinstance(body, dict):
                return {k: shape(v) for k, v in body.items()}
            if isinstance(body, list):
                return [shape(body[0])] if body else []
            return type(body).__name__
        order_id = cases[0][1]["rail_order_id"]
        status = rail.get_order(order_id) if order_id else None
        print("\nMND-01 Testnet3 contract (values redacted):", json.dumps({
            "create_order_status": cases[0][1]["stage"],
            "order_status": status.status if status else None,
            "order_outcome": status.outcome.value if status else None,
            "order_fields": shape(status.raw) if status else None}, indent=2))

    bal, chain = ledger.balances(), ledger.verify_chain()
    ok &= chain["intact"] and bal["available"] + bal["held"] + bal["settled"] == bal["budget"]
    stored = [{"id": p["id"], "request_key": p["request_key"], "seller_domain": p["seller_domain"],
               "item": p["item"], "amount": format_milli(p["amount"]), "amount_milli": p["amount"],
               "stage": p["stage"], "rail_order_id": p["rail_order_id"], "delivery": p["delivery"]}
              for p in ledger.purchases()]
    print("\nreceipt", json.dumps({"rail": rail.name, "synthetic_seller": True, "purchases": stored,
                                   "balances_milli": bal, "chain": chain}, indent=2))
    ledger.conn.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
