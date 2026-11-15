# Backend owner controls (MND-14)

Owner HTTP requests require X-Owner-Token. An agent session key does not authorize
policy edits, pause or resume. This issue changes backend behavior only; frontend
rendering and client wiring are outside the requested scope.

## Policy edits

Read `GET /api/v1/state` using the owner credential and retain `policy.version`.
Include it as a required positive integer `expected_version` in every HTTP patch:

```json
{"expected_version":3,"max_single_spend":"0.080"}
```

The gateway compares that version inside the policy update transaction. A stale
version returns HTTP 409 without changing policy, version or evidence. Missing,
noninteger, boolean and nonpositive versions return HTTP 422. Successful updates
increment the current version once and append the policy snapshot atomically.
Two independent connections editing the same version have at most one winner.

This is an intentional HTTP contract change: older backend clients must refresh
state and send expected_version. Existing optional policy fields are unchanged;
monetary values remain decimal strings at the HTTP boundary and integer milli
inside the ledger. The trusted Python bootstrap helper may omit expected_version
for compatibility; external HTTP requests cannot omit it.

## Pause, exposure and resume

Pause is checked inside dispatch claim acquisition. After an acknowledged pause,
no new claim may be written. Active session keys are revoked in the same pause
transaction. API pause responses and audit text explicitly state that payments
already dispatched may still complete and their unresolved funds remain held.
Pause does not cancel a remote payment or suppress its eventual settlement.

An owner resume request returns HTTP 409 while any purchase is DISPATCHED or
RECOVERY_UNKNOWN, or configured wallet monitoring reports a mismatch or inadequate
allocation coverage. Reconcile original orders and supply appropriate owner wallet
observations before resuming. Rejected resume attempts do not change the pause,
policy version, evidence or balances. Repeating a safe resume is a no-op.

Pending approval holds alone do not prove remote exposure and do not prevent
resume. Paid-but-undelivered purchases remain spent; delivery recovery is separate.
Resume never revives revoked session keys; the owner must issue a new capability.

These checks establish consistent local accounting state. Optional owner-recorded
wallet monitoring is not an independent live balance verification or a freshness
proof. Provider approval/resume integration remains MND-10; no GOAT call or real
payment is needed to test these local controls. Frontend clients must separately
adopt versioned writes and display the backend pause notice.

## Verification

```text
python -m pytest backend/tests/test_owner_controls.py -q
python -m pytest backend/tests -q
python -m ruff check backend scripts
python scripts/smoke.py
```

Cases cover stale/concurrent owner edits, strict version inputs, audit rollback,
post-acknowledgement pause races, unresolved/unknown payments, wallet mismatch,
owner-only resume and settlement after pause.
