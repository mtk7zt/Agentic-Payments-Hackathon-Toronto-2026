# Sprint plan

36 hours, three builders. Ticket keys are planning keys; Linear assigns its own identifiers.

| Sprint | Window | Tickets |
| --- | --- | --- |
| 1 · Rail Verification & Payment Core | 0–8 h | MND-01, MND-02, MND-03, MND-27 |
| 2 · Mandate Engine & Attack Interception | 8–20 h | MND-04 to MND-10, MND-28 |
| 3 · Cockpit & Live Stream | 20–30 h | MND-11 to MND-18 |
| 4 · Exploit Scenarios, Pitch Rehearsal & Judge PDF | 30–36 h | MND-19 to MND-26, MND-29 |

MND-03 (isolated executor and repository baseline) was not named in the sprint brief. It sits in Sprint 1 because MND-04 depends on it.

## Already scaffolded in this repository

| Ticket | State |
| --- | --- |
| MND-02 money types | Done in `backend/models/money.py`, tested |
| MND-04 rail adapter | GOAT Flow and mock adapters written; live call not yet made |
| MND-06 idempotent intents | Full argument binding, fail-closed owner auth and bounded model-callback runner implemented locally; review pending |
| MND-07 policy evaluator | Expiry, argument constraints and approval/dispatch rechecks implemented locally; review pending |
| MND-08 atomic allocations | Atomic reservations plus owner-recorded wallet monitoring implemented locally; review pending; no live balance verification |
| MND-09 dispatch claims | Crash/reopen and concurrent claim/settlement proofs added locally; review pending |
| MND-11 unknown-outcome reconciliation | `orchestrator.reconcile`, tested on the mock rail |
| MND-12 delivery validation | Schema and request-match checks; freshness not yet |
| MND-13 hash-linked evidence | Chain and verifier done; external checkpoint not yet |
| MND-14 kill-switch | Done, tested |
| MND-15, 16, 18, 19, 20 UI | Built against the simulated ledger; not yet wired to the API |
| MND-17 event stream | SSE endpoint with Last-Event-ID resume; no auth on it yet |
| MND-27 CLI smoke test | `scripts/smoke.py`, mock mode passing |
| MND-28 session keys | Issue and revoke done; freeze state exists but nothing sets it yet |
