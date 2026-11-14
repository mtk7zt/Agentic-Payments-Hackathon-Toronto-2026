"""Deterministic policy engine.

`evaluate` is a pure function of (intent, policy, available allocation). It
never sees model text, so nothing the agent reads can change its answer.
Reservations live in core/ledger.py and session keys in core/sessions.py;
`backend.core` re-exports all three as one import surface.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum


class Decision(str, Enum):
    AUTO_APPROVED = "AUTO_APPROVED"
    HELD_FOR_REVIEW = "HELD_FOR_REVIEW"
    BLOCKED = "BLOCKED"


class Clause(str, Enum):
    WITHIN_AUTO_APPROVE_LIMIT = "WITHIN_AUTO_APPROVE_LIMIT"
    ABOVE_AUTO_APPROVE_LIMIT = "ABOVE_AUTO_APPROVE_LIMIT"
    VENDOR_NOT_WHITELISTED = "VENDOR_NOT_WHITELISTED"
    VENDOR_ADDRESS_MISMATCH = "VENDOR_ADDRESS_MISMATCH"
    VENDOR_UNLISTED_NEEDS_REVIEW = "VENDOR_UNLISTED_NEEDS_REVIEW"
    ABOVE_TRANSACTION_CEILING = "ABOVE_TRANSACTION_CEILING"
    ATTEMPT_CEILING_REACHED = "ATTEMPT_CEILING_REACHED"
    INSUFFICIENT_ALLOCATION = "INSUFFICIENT_ALLOCATION"
    AGENT_FROZEN = "AGENT_FROZEN"
    SESSION_KEY_INVALID = "SESSION_KEY_INVALID"


CLAUSE_TEXT: dict[Clause, str] = {
    Clause.WITHIN_AUTO_APPROVE_LIMIT: "Amount is within the auto-approve limit and the seller is whitelisted.",
    Clause.ABOVE_AUTO_APPROVE_LIMIT: "Amount is above the auto-approve limit, so a person must authorize it.",
    Clause.VENDOR_NOT_WHITELISTED: "The seller is not on this mandate's whitelist.",
    Clause.VENDOR_ADDRESS_MISMATCH: "The seller is whitelisted, but the payment address is not the one on file.",
    Clause.VENDOR_UNLISTED_NEEDS_REVIEW: "The seller is not whitelisted and strict mode is off, so a person must review it.",
    Clause.ABOVE_TRANSACTION_CEILING: "Amount is above the hard cap for a single purchase.",
    Clause.ATTEMPT_CEILING_REACHED: "This task has used all of its allowed paid attempts.",
    Clause.INSUFFICIENT_ALLOCATION: "Not enough of the task budget is left once held amounts are set aside.",
    Clause.AGENT_FROZEN: "The kill-switch is engaged. No purchase may be dispatched.",
    Clause.SESSION_KEY_INVALID: "The agent's session key is missing, frozen or revoked.",
}


@dataclass(frozen=True)
class Policy:
    """All amounts are integer thousandths of USD-TDN."""

    version: int = 1
    max_single_spend: int = 100  # auto-approve ceiling: 0.100
    transaction_ceiling: int = 3_000  # hard cap; approval cannot override
    task_budget_cap: int = 5_000
    max_attempts: int = 5
    strict_whitelist: bool = True
    paused: bool = False
    # seller domain -> payment address on file (lower-case)
    whitelisted_sellers: dict[str, str] = field(default_factory=dict)

    def canonical(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def hash(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()


@dataclass(frozen=True)
class Intent:
    seller_domain: str
    seller_address: str
    item: str
    amount: int
    attempt: int = 1


@dataclass(frozen=True)
class Evaluation:
    decision: Decision
    clause: Clause
    reason: str
    policy_version: int
    policy_hash: str


def evaluate(intent: Intent, policy: Policy, available: int) -> Evaluation:
    """Hard constraints first; order is part of the contract and is tested."""

    def out(decision: Decision, clause: Clause) -> Evaluation:
        return Evaluation(decision, clause, CLAUSE_TEXT[clause], policy.version, policy.hash())

    if isinstance(intent.amount, bool) or not isinstance(intent.amount, int) or intent.amount <= 0:
        raise ValueError("intent.amount must be a positive int (thousandths)")

    domain = intent.seller_domain.strip().lower()
    address = intent.seller_address.strip().lower()
    on_file = policy.whitelisted_sellers.get(domain)

    if policy.paused:
        return out(Decision.BLOCKED, Clause.AGENT_FROZEN)
    if on_file is not None and on_file != address:
        # A listed name with a different address is a redirect, never a review.
        return out(Decision.BLOCKED, Clause.VENDOR_ADDRESS_MISMATCH)
    if on_file is None and policy.strict_whitelist:
        return out(Decision.BLOCKED, Clause.VENDOR_NOT_WHITELISTED)
    if intent.attempt > policy.max_attempts:
        return out(Decision.BLOCKED, Clause.ATTEMPT_CEILING_REACHED)
    if intent.amount > policy.transaction_ceiling:
        return out(Decision.BLOCKED, Clause.ABOVE_TRANSACTION_CEILING)
    if intent.amount > available:
        return out(Decision.BLOCKED, Clause.INSUFFICIENT_ALLOCATION)
    if on_file is None:
        return out(Decision.HELD_FOR_REVIEW, Clause.VENDOR_UNLISTED_NEEDS_REVIEW)
    if intent.amount > policy.max_single_spend:
        return out(Decision.HELD_FOR_REVIEW, Clause.ABOVE_AUTO_APPROVE_LIMIT)
    return out(Decision.AUTO_APPROVED, Clause.WITHIN_AUTO_APPROVE_LIMIT)

