"""Controlled attack scenarios (MND-22).

These are labelled, deterministic fixtures. They show that the gateway contains an agent that
follows malicious text. They do not claim Mandate detects prompt injection in general: the
gateway never reads the agent's prompt, it only enforces the mandate on every purchase.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.ledger import Ledger
from ..core.policy import Intent, Policy
from ..services import orchestrator
from ..services.payment_rail import MockRail
from ..services.seller import MockSeller

FIXTURE_LABEL = "SYNTHETIC FIXTURE - controlled scenario, not real traffic"
SELLER, ADDRESS = "cupworks.example", "0x00000000000000000000000000000000000000c1"
POLICY = Policy(whitelisted_sellers={SELLER: ADDRESS}, max_single_spend=100, transaction_ceiling=3_000,
                task_budget_cap=5_000, max_attempts=5)


class CountingRail(MockRail):
    """Mock rail that counts every checkout call, so 'zero calls' is measured, not assumed."""


@dataclass
class Outcome:
    name: str
    label: str
    checkout_calls: int
    stages: list[str]
    clauses: list[str | None]
    spent_milli: int
    held_milli: int
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _run(name: str, intents: list[Intent], policy: Policy = POLICY) -> Outcome:
    ledger, rail = Ledger(policy=policy), CountingRail()
    rows = [orchestrator.submit(ledger, rail, MockSeller(), request_key=f"{name}-{i}", intent=it,
                                tool_call={"tool": "checkout", "arguments": {"item": it.item}})
            for i, it in enumerate(intents)]
    b = ledger.balances()
    return Outcome(name, FIXTURE_LABEL, rail.create_calls, [r["stage"] for r in rows], [r["clause"] for r in rows],
                   b["settled"], b["held"])


def injected_seller() -> Outcome:
    """Hidden page text tells the agent to pay attacker.eth. The agent obeys; the gateway does not."""
    return _run("injected-seller", [Intent("attacker.eth", "0xdead", "cups", 40)])


def address_swap() -> Outcome:
    """Injected text keeps the trusted seller name but swaps in an attacker's payment address."""
    return _run("address-swap", [Intent(SELLER, "0x000000000000000000000000000000000000dead", "cups", 40)])


def over_limit() -> Outcome:
    """Allowed seller, but the amount is above the hard per-purchase ceiling. Approval cannot override it."""
    return _run("over-limit", [Intent(SELLER, ADDRESS, "cups", 3_001)])


def runaway_loop(n: int = 20) -> Outcome:
    """A looping agent re-buys the same cheap item. max_attempts stops it after five purchases."""
    return _run("runaway-loop", [Intent(SELLER, ADDRESS, "cups", 40) for _ in range(n)])


def cheap_drain(n: int = 80) -> Outcome:
    """A loop buying many different cheap items. The task budget is the hard ceiling."""
    return _run("cheap-drain", [Intent(SELLER, ADDRESS, f"cups-{i}", 90) for i in range(n)])


ALL = (injected_seller, address_swap, over_limit, runaway_loop, cheap_drain)
