"""Deterministic policy engine.

`evaluate` is a pure function of (intent, policy, available allocation). It
never sees model text, so nothing the agent reads can change its answer.
Reservations live in core/ledger.py and session keys in core/sessions.py;
`backend.core` re-exports all three as one import surface.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


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
    POLICY_EXPIRED = "POLICY_EXPIRED"
    ARGUMENT_CONSTRAINT_VIOLATION = "ARGUMENT_CONSTRAINT_VIOLATION"


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
    Clause.POLICY_EXPIRED: "The mandate has expired. No purchase may be authorized.",
    Clause.ARGUMENT_CONSTRAINT_VIOLATION: "The tool arguments do not match this mandate's required values.",
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
    # Rolling window for counting repeat purchases of the same item from the same seller.
    # A loop re-buying inside the window hits max_attempts; a weekly household order does not.
    repeat_window_s: int = 86_400
    strict_whitelist: bool = True
    paused: bool = False
    # seller domain -> payment address on file (lower-case)
    whitelisted_sellers: dict[str, str] = field(default_factory=dict)

    # Unix seconds; None preserves mandates without a time limit.
    expires_at: float | None = None
    # Required top-level tool argument keys with exact JSON values.
    argument_constraints: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.expires_at is not None and not _finite_timestamp(self.expires_at):
            raise ValueError("policy.expires_at must be finite Unix seconds or None")

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
    arguments: dict[str, Any] | None = None


@dataclass(frozen=True)
class Evaluation:
    decision: Decision
    clause: Clause
    reason: str
    policy_version: int
    policy_hash: str


def _finite_timestamp(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _arguments_match(arguments: Any, constraints: dict[str, Any]) -> bool:
    if not constraints:
        return True
    if not isinstance(arguments, dict):
        return False
    try:
        # JSON comparison distinguishes booleans, integers, strings and floats;
        # nested object order is immaterial, while array order is significant.
        return all(key in arguments and
                   json.dumps(arguments[key], sort_keys=True, allow_nan=False) ==
                   json.dumps(value, sort_keys=True, allow_nan=False)
                   for key, value in constraints.items())
    except (TypeError, ValueError):
        return False


def evaluate(intent: Intent, policy: Policy, available: int, *, now: float | None = None) -> Evaluation:
    """Hard constraints first; no model text or implicit clock influences the result.

    An expiring policy requires explicit finite ``now`` in Unix seconds. The
    expiry boundary is exclusive: at expires_at the authority is already gone.
    """

    def out(decision: Decision, clause: Clause) -> Evaluation:
        return Evaluation(decision, clause, CLAUSE_TEXT[clause], policy.version, policy.hash())

    if isinstance(intent.amount, bool) or not isinstance(intent.amount, int) or intent.amount <= 0:
        raise ValueError("intent.amount must be a positive int (thousandths)")

    if isinstance(intent.attempt, bool) or not isinstance(intent.attempt, int) or intent.attempt <= 0:
        raise ValueError("intent.attempt must be a positive int")
    if policy.expires_at is not None and not _finite_timestamp(now):
        raise ValueError("now must be explicit finite Unix seconds for an expiring policy")

    domain = intent.seller_domain.strip().lower()
    address = intent.seller_address.strip().lower()
    on_file = policy.whitelisted_sellers.get(domain)

    if policy.paused:
        return out(Decision.BLOCKED, Clause.AGENT_FROZEN)
    if policy.expires_at is not None and now >= policy.expires_at:
        return out(Decision.BLOCKED, Clause.POLICY_EXPIRED)
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
    if not _arguments_match(intent.arguments, policy.argument_constraints):
        return out(Decision.BLOCKED, Clause.ARGUMENT_CONSTRAINT_VIOLATION)
    if on_file is None:
        return out(Decision.HELD_FOR_REVIEW, Clause.VENDOR_UNLISTED_NEEDS_REVIEW)
    if intent.amount > policy.max_single_spend:
        return out(Decision.HELD_FOR_REVIEW, Clause.ABOVE_AUTO_APPROVE_LIMIT)
    return out(Decision.AUTO_APPROVED, Clause.WITHIN_AUTO_APPROVE_LIMIT)

