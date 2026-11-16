# Locally signed evidence checkpoints (MND-13)

The event chain retains unique contiguous sequence numbers, previous hashes,
policy hashes and purchase references. SQLite triggers prevent application UPDATE
or DELETE on events and checkpoints. A database administrator can bypass these
triggers; this is not an immutable log.

Optionally set `MANDATE_CHECKPOINT_KEY` privately on the gateway. Use a separate,
high-entropy signing key; do not put it in the database, public config, browser,
repository or exports. The committed env example contains only an empty entry.
The application creates HMAC-SHA256 checkpoints every ten events, in the same
transaction as the event. A checkpoint write failure rolls back that transaction.
Manual owner checkpoints capture the current head. Signing refuses a broken chain
or history inconsistent with the previous signed checkpoint.

A checkpoint binds schema, configured mandate/agent, sequence, head hash, issue
time and key identifier. Its reference identifies the signed artifact; no signing
key, owner token or credential is exported. HMAC provides local symmetric trust,
not a publicly verifiable third-party signature. A holder of the key can forge
checkpoints; keep the key separate from the database.

Owner endpoints require X-Owner-Token:

| Endpoint | Behavior |
| --- | --- |
| POST /api/v1/evidence/checkpoints | Sign and retain the current head; 409 if unconfigured/broken. |
| GET /api/v1/evidence/checkpoint | Latest local checkpoint, or an explicit missing-checkpoint result. |
| GET /api/v1/evidence/verify | Check links and latest retained local checkpoint. |
| POST /api/v1/evidence/verify | Verify against an owner-retained `{"checkpoint": {...}}` artifact. |
| GET /api/v1/purchases/{id}/evidence | Includes the latest checkpoint and its reference, or null. |

Copy the checkpoint to an owner-controlled location when exporting evidence, then
supply that artifact to POST verify in later reviews. Normal link corruption
reports the first broken sequence. Truncation before a retained checkpoint reports
the first missing sequence. A full, internally consistent rewrite reports a
mismatch at the retained checkpoint boundary; it cannot identify the original
first edited row without an older trusted history.

Unconfigured signing does not invent trust: `checkpoint_verified` remains false.
An existing checkpoint with an unavailable/wrong signing key fails verification;
restore the original key for that history. Key rotation/key-ring management is
outside this demo. Events after the last signed checkpoint have valid hash links
but are not covered by that signature until the next checkpoint.

Independent retention is deferred. Deleting/rolling back the entire database,
including every local checkpoint, cannot be detected without an externally retained
checkpoint (or another independent expected head). Compromise of both the signing
key and database defeats this local trust model. A purchase-filtered event export
alone is not a complete chain; verification uses the gateway's complete ledger and
the retained checkpoint. None of this establishes service quality or live payment
finality.

Verification:

```text
python -m pytest backend/tests/test_checkpoints.py -q
python -m pytest backend/tests -q
ruff check backend scripts
```
