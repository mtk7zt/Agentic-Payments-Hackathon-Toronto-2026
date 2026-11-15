# Deterministic mandate constraints (MND-07)

`backend.core.policy.evaluate` decides from structured intent, policy, available
allocation and an explicit clock value. It never interprets instructions in tool
arguments. Monetary amounts are positive integer thousandths of USD-TDN, and
attempts are positive integers. Invalid numeric input raises `ValueError` before
a decision; the gateway's existing money parser validates agent input first.

Checks run in this order: paused mandate, expiry, listed seller address mismatch,
strict seller whitelist, attempt ceiling, transaction ceiling, available allocation,
argument constraints, then seller/amount rules that require review. Seller domains
and submitted addresses are trimmed and lowercased, with exact domain matching;
there is no wildcard or suffix match. Whitelist entries use the existing normalized
lowercase representation.

## Expiry

`Policy.expires_at` is optional Unix seconds, default `None` (no expiry). It must be
a finite number, never a boolean. When configured, the pure evaluator requires an
explicit finite `now` keyword argument. `now >= expires_at` returns `BLOCKED` with
`POLICY_EXPIRED`. The ledger supplies its current clock inside each transaction.

## Argument constraints

`Policy.argument_constraints` defaults to `{}` (no argument restrictions). It maps
required top-level tool argument names to exact JSON values, for example:

```python
Policy(
    whitelisted_sellers={"seller-a.example": "0xa1"},
    expires_at=1_800_000_000,
    argument_constraints={"region": "CA", "count": 1},
)
```

Every configured key must exist and match. Strings, integers, floats and booleans
are distinct; nested objects compare independent of key order; arrays retain
order. Nested values match in full, so additional nested keys are changes.
Unconfigured top-level keys are allowed. Missing/non-object arguments or invalid
JSON values in a constrained field produce `BLOCKED` with
`ARGUMENT_CONSTRAINT_VIOLATION`. These are explicit equality rules, not a JSON
Schema implementation or model-based risk score.

`Intent.arguments` follows the existing positional `attempt` field. Ledger
execution evaluates the arguments from persisted `tool_call["arguments"]`, so a
separate in-memory intent cannot misrepresent the executable arguments. Constraints
and expiry participate in the canonical policy snapshot and hash and round-trip
through SQLite. Existing snapshots without either field retain the unset defaults.
Configure the fields through `Policy` or owner-side `Ledger.set_policy`; the current
HTTP `PolicyPatch` and demo UI do not expose these additional configuration fields.

## Approval and dispatch

New hard denials reserve no money and cannot be approved. Before approving or
claiming dispatch, the ledger re-evaluates the persisted purchase against the current
policy and time. It adds that purchase's own existing hold back to available
allocation for the check, once. A hard denial raises `LedgerError` containing the
stable reason code and explanation; an ordinary human approval never overrides it.
Dispatch also refuses a newly required soft review unless the purchase has human
approval. No dispatch claim is recorded on refusal.

A refused approval/dispatch retains the existing hold and stage. Pending approval
holds can be explicitly rejected or expired through the existing release path;
refused reserved purchases remain held for owner/recovery handling. This change
does not invent a payment cancellation operation or release a potentially live
payment. The initial evaluated decision remains in audit evidence; a failed recheck
rolls back its transaction and does not append a misleading approval/dispatch event.
