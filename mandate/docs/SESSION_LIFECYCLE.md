# Backend session-key lifecycle (MND-28)

Owner routes require `X-Owner-Token` and fail closed when the owner token is
unconfigured. Agent routes use `X-Session-Key`. Tokens are returned once on
issuance; SQLite stores their SHA-256 digest, never the token itself.

| Owner endpoint | Purpose |
| --- | --- |
| `POST /api/v1/session-keys` | Issue an active key for the configured agent; refused while paused. |
| `GET /api/v1/session-keys` | List key ID, agent ID and state, excluding tokens and digests. |
| `POST /api/v1/session-keys/{key_id}/state` | Submit `{"state":"frozen"}`, `{"state":"active"}` or `{"state":"revoked"}`. |

Freeze temporarily disables the key. Only the owner can thaw it, and thaw is
refused while paused. Revocation is permanent; attempts to change a revoked key
return HTTP 409. Repeating an unchanged non-revoked state is a no-op. Unknown or
foreign-agent keys return 409 without writes. Invalid state values return 422.
State changes and their audit events commit together or roll back together.

The kill-switch revokes both active and frozen keys in its pause transaction.
Resume does not revive them. A blocked unapproved seller or mismatched seller
address revokes only the proposing key in the purchase-block transaction; no
checkout call or reservation occurs for that blocked purchase.
The offline smoke fixture explicitly issues a new owner key after its attack
scenario before continuing independent delivery/timeout scenarios; this is
labelled MOCK and never gives an agent automatic credential renewal.

The ledger rechecks the proposing key's agent and active state inside proposal,
reservation, human approval and dispatch-claim transactions. A freeze or revoke
acknowledged before a claim prevents that claim, even when HTTP authentication
had already succeeded. A claim committed before revocation represents in-flight
exposure: revocation cannot cancel it, and settlement/reconciliation remain
allowed. Existing holds are retained; pending approvals can still be rejected by
the owner. Trusted in-process test/bootstrap calls may omit a session ID; HTTP
proposals always bind the authenticated key.

This is backend-only functionality. UI presentation of active/frozen/revoked
states remains outside scope. The application uses the current single-tenant,
single-agent ledger; these controls do not establish process or filesystem
isolation (MND-03) or live provider authorization.

From the `mandate` directory:

```text
python -m pytest backend/tests/test_session_lifecycle.py -q
python -m pytest backend/tests -q
python -m ruff check backend scripts
python scripts/smoke.py
```
