#!/usr/bin/env python3
"""MND-27 · CLI smoke test for checkout and receipt logging.

    python scripts/smoke.py            # mock rail, no network, no money
    python scripts/smoke.py --live     # ONE real GOAT Flow Testnet3 order (needs GOAT_FLOW_* env)

Exits non-zero if any purchase ends in an unexpected state or the evidence chain is broken.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

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
    args = ap.parse_args()

    ledger = Ledger(":memory:")
    ledger.set_policy(whitelisted_sellers=dict([A, B]))
    key_id, _ = ledger.issue_session_key()
    seller = MockSeller(fail_items={"lane-rates"})
    rail = GoatFlowRail(GoatFlowConfig.from_env()) if args.live else MockRail({"0005": RailOutcome.UNKNOWN})

    def buy(n, dom, addr, item, amount):
        return orchestrator.submit(ledger, rail, seller, request_key=f"smoke-{n:04d}-key",
                                   intent=Intent(dom, addr, item, amount),
                                   tool_call={"tool": "buy_supplier_data", "vendor": dom, "item": item},
                                   session_key_id=key_id)

    if args.live:
        cases = [("micro purchase", buy(1, *A, "shipping-estimate", 40), None)]
    else:
        cases = [
            ("micro purchase", buy(1, *A, "shipping-estimate", 40), "VERIFIED"),
            ("above limit", buy(2, *B, "shipping-matrix-2026", 2500), "PENDING_APPROVAL"),
            ("injected redirect", buy(3, "attacker.eth", "0xdead", "shipping-matrix-2026", 2500), "BLOCKED"),
            ("seller fails after payment", buy(4, *B, "lane-rates", 60), "RECOVERY_UNDELIVERED"),
            ("rail timeout", buy(5, *A, "customs-codes", 80), "RECOVERY_UNKNOWN"),
        ]

    ok = True
    for name, p, want in cases:
        good = want is None or p["stage"] == want
        ok &= good
        print(f"{'PASS' if good else 'FAIL'}  {name:28} {format_milli(p['amount']):>7}  {p['stage']:22} "
              f"{p['clause'] or ''}  order={p['rail_order_id'] or '-'}")

    if not args.live:
        calls_before = rail.create_calls
        try:
            orchestrator.execute(ledger, rail, seller, cases[4][1]["id"])  # a blind retry must be refused
            ok = False
            print("FAIL  retry after timeout was allowed")
        except LedgerError as exc:
            print(f"PASS  retry after timeout refused       ({exc}); rail calls unchanged: {rail.create_calls == calls_before}")
            ok &= rail.create_calls == calls_before

    bal, chain = ledger.balances(), ledger.verify_chain()
    ok &= chain["intact"] and bal["available"] + bal["held"] + bal["settled"] == bal["budget"]
    print("\nreceipt", json.dumps({"rail": rail.name, "balances_milli": bal, "chain": chain}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
