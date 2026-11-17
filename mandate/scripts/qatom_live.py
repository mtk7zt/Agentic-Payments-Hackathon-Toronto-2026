#!/usr/bin/env python3
"""One real Qatom purchase through Mandate's full pipeline, with your approval.

    py -m scripts.qatom_live --catalog-id 26 --amount 0.001

What happens:
  1. Mandate evaluates the purchase against its rules and holds the amount.
  2. It records the dispatch claim, then calls Qatom's agent checkout.
  3. Qatom asks you to approve. This prints the approval link; open it and approve.
  4. Mandate polls the ORIGINAL approval until Qatom reports the transaction, then settles once.

The ledger is stored in DATABASE_URL (or ./mandate-live.db), so a pending approval survives
if you stop the script; run it again with --resume <purchase id> to continue.
Real money: the amount is capped at 0.100 and must not exceed what Qatom charges for the item.
Needs a prior `py -m scripts.qatom_login`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from backend.core.ledger import Ledger, Stage
from backend.core.policy import Intent
from backend.models.money import format_milli, parse_milli
from backend.services import orchestrator
from backend.services.payment_rail import RailResult
from backend.services.qatom_rail import QatomConfig, QatomRail

MAX_MILLI = 100  # 0.100: the owner's standing limit for live tests
SELLER_DOMAIN = "qatom.market"
OPEN = (Stage.DISPATCHED.value, Stage.RECOVERY_UNKNOWN.value)


class _Recording:
    """Wraps the rail to show the approval link to the owner once. The link is never stored."""

    def __init__(self, rail: QatomRail):
        self.rail, self.name, self.shown = rail, rail.name, False

    def _show(self, res: RailResult) -> RailResult:
        url = (res.raw or {}).get("approval_url")
        if url and not self.shown:
            print(f"\nApprove this purchase in Qatom:\n  {url}\n", flush=True)
            self.shown = True
        return res

    def create_order(self, **kw: Any) -> RailResult:
        return self._show(self.rail.create_order(**kw))

    def get_order(self, order_id: str) -> RailResult:
        return self._show(self.rail.get_order(order_id))

    def get_proof(self, order_id: str) -> dict[str, Any]:
        return self.rail.get_proof(order_id)


def run(ledger: Ledger, rail: Any, *, catalog_id: int, amount_milli: int, resume: str | None = None,
        arguments: dict[str, str] | None = None, wait_s: float = 300, poll_s: float = 5, sleep=time.sleep, clock=time.monotonic) -> dict[str, Any]:
    item = f"qatom-{catalog_id}"
    address = f"catalog:{catalog_id}"
    if resume:
        pid = resume
    else:
        pol = ledger.policy()
        if pol.whitelisted_sellers.get(SELLER_DOMAIN) != address or pol.max_single_spend < amount_milli:
            ledger.set_policy(expected_version=pol.version,
                              whitelisted_sellers={**pol.whitelisted_sellers, SELLER_DOMAIN: address},
                              max_single_spend=max(pol.max_single_spend, amount_milli))
        p = orchestrator.submit(ledger, rail, None, request_key=f"qatom-live-{catalog_id}-{int(time.time())}",
                                intent=Intent(SELLER_DOMAIN, address, item, amount_milli),
                                tool_call={"tool": "qatom_checkout", "catalog_item_id": catalog_id,
                                           **({"arguments": arguments} if arguments else {})})
        pid = p["id"]
    deadline = clock() + wait_s
    p = ledger.purchase(pid)
    while p["stage"] in OPEN and clock() < deadline:
        sleep(poll_s)
        p = orchestrator.reconcile(ledger, rail, None, pid)
    return p


def _parse_args(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not name.strip():
            raise ValueError(f"--arg needs NAME=VALUE, got {pair!r}")
        out[name.strip()] = value
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="One real Qatom purchase through Mandate, approved by you.")
    ap.add_argument("--catalog-id", type=int, required=True, help="Qatom catalog item id, for example 26")
    ap.add_argument("--amount", required=True, help='the most you allow, as a decimal string, for example "0.001"')
    ap.add_argument("--resume", help="continue an earlier purchase id instead of starting a new one")
    ap.add_argument("--arg", action="append", default=[], metavar="NAME=VALUE",
                    help="an input the item needs, for example --arg package=react (repeat for more)")
    ap.add_argument("--wait", type=float, default=300, help="seconds to wait for your approval (default 300)")
    args = ap.parse_args(argv)
    amount = parse_milli(args.amount)
    try:
        item_args = _parse_args(args.arg)
    except ValueError as exc:
        print(f"Refusing: {exc}", file=sys.stderr)
        return 2
    if amount > MAX_MILLI:
        print(f"Refusing: {args.amount} is above the 0.100 live-test limit.", file=sys.stderr)
        return 2
    cfg = QatomConfig.from_env()
    cfg = QatomConfig(mcp_url=cfg.mcp_url, token_file=cfg.token_file, item_map={f"qatom-{args.catalog_id}": args.catalog_id},
                      timeout_s=cfg.timeout_s)
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("MANDATE_DB") or "sqlite:///mandate-live.db"
    ledger = Ledger(db_url, mandate_id="mnd_live_qatom")
    rail = _Recording(QatomRail(cfg, store=ledger))
    p = run(ledger, rail, catalog_id=args.catalog_id, amount_milli=amount, resume=args.resume,
            arguments=item_args, wait_s=args.wait)
    out = {k: p[k] for k in ("id", "stage", "clause", "rail", "approval_id", "rail_tx_id")}
    out["amount"] = format_milli(p["amount"])
    print(json.dumps(out, indent=2))
    print("evidence chain intact:", ledger.verify_chain()["intact"])
    if p["stage"] in OPEN:
        print(f"\nStill waiting for approval. Approve, then run: py -m scripts.qatom_live --catalog-id "
              f"{args.catalog_id} --amount {args.amount} --resume {p['id']}")
        return 3
    return 0 if p["stage"] == Stage.SETTLED.value else 1


if __name__ == "__main__":
    sys.exit(main())
