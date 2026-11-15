# Delivery validation (MND-12)

Payment confirmation and delivery validation remain separate. The orchestrator
commits settlement before fetching or validating seller output. Invalid, stale,
missing or oversized output enters RECOVERY_UNDELIVERED with delivery FAILED;
the settled amount remains charged. Reconciliation retries only delivery against
the original order. It never creates a second payment.

`verify_delivery(payload, item=..., request_id=..., rail_order_id=...)` returns
Delivery(ok, detail). It verifies:

- A JSON object containing the original item/request IDs, structured data and a
  timezone-aware ISO 8601 created_at timestamp. The orchestrator also requires
  the original settled rail order ID. Unexpected top-level fields are rejected.
- Maximum complete JSON payload size of 64,000 UTF-8 bytes, including metadata.
  Non-JSON values and non-finite numbers are rejected. The HTTP adapter limits
  streamed response bytes before JSON decoding and closes rejected responses.
- Timestamp age no greater than 300 seconds, with at most 30 seconds future
  clock skew. Exact boundary values pass. Validation accepts an explicit clock
  and alternate finite nonnegative limits for deterministic tests.
- Supported schema version and boolean synthetic marker when supplied. Optional
  decimal price and integer amount_milli metadata must be supplied as a matching
  pair and obey the existing safe integer-thousandth contract.

The in-process mock now returns timestamps, schema version, synthetic labels and
the original order reference, so offline scenarios use the same validation path.
The controlled HTTP seller already returns these fields.

Seller text inside structured data remains untrusted content. Validation does
not execute it, grant authority from it, or claim it is true. A seller-provided
timestamp is not an independent cryptographic proof of freshness; this check
rejects obviously stale/future delivery according to the supplied clock.
Matching price metadata does not itself prove a charge or bind catalog pricing
before payment. Provider receipt verification and pre-dispatch price binding
remain separate concerns.

## Verification

Run from this worktree's mandate directory using its configured Python runtime:

```powershell
python -m pytest backend/tests/test_delivery_validation.py -q
python -m pytest backend/tests -q
python -m ruff check backend scripts
python scripts/smoke.py
```

All tests use mock rails; no GOAT credentials, inference API or live transfer is
needed. New cases cover freshness boundaries, missing/naive timestamps, request
and order mismatch, JSON/schema and UTF-8 size, inert injected text and a stale
paid response recovering through fresh delivery with exactly one original charge.
