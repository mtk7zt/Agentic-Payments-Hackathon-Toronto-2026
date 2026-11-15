# Sprint 2 local review: MND-06 through MND-09

The source checkout is on `feature/sprint-2-integration`, a local verification
branch combining the four isolated issue implementations. It has not been pushed
or merged into main. Each issue retains its own review branch:

| Issue | Branch | Issue commit |
| --- | --- | --- |
| MND-06 / VEN-11 | feature/MND-06-bounded-agent-intents | cd69e12 (follows 08b8813) |
| MND-07 / VEN-12 | feature/MND-07-policy-constraints | 24c078e |
| MND-08 / VEN-13 | feature/MND-08-wallet-allocations | 8e2b5ee |
| MND-09 / VEN-14 | feature/MND-09-durable-dispatch | 0528de5 |

The integration branch preserves both the current-policy recheck and the wallet
guard inside the dispatch-claim transaction. It includes additional cross-issue
tests and behavior-preserving lint fixes. The smoke retry check now specifically
expects LedgerError rather than treating arbitrary exceptions as proof of safety.

## Verification

Run from `mandate` in the source checkout:

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests -q
.\.venv\Scripts\python.exe -m ruff check backend scripts
.\.venv\Scripts\python.exe scripts/smoke.py
cd frontend
npm run typecheck
```

Latest combined backend result: 199 passed, including all original 28 cases.
Ruff over the entire backend/scripts tree, TypeScript and offline smoke pass.
The existing Starlette/httpx deprecation warning is a dependency warning, not a
failing test. Test configuration forces mock integration defaults before gateway
import, even in a shell configured for live GOAT or HTTP seller modes.

Focused issue checks from `mandate`:

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/test_agent_intents.py backend/tests/test_agent_runner.py -q
.\.venv\Scripts\python.exe -m pytest backend/tests/test_policy_constraints.py -q
.\.venv\Scripts\python.exe -m pytest backend/tests/test_wallet_allocations.py backend/tests/test_wallet_api.py -q
.\.venv\Scripts\python.exe -m pytest backend/tests/test_durable_dispatch.py -q
.\.venv\Scripts\python.exe -m pytest backend/tests/test_sprint2_integration.py -q
```

Independent review found a delayed-worker proposal could start after the runner
deadline. Three reproducing regressions failed before the fix. The worker now
checks the absolute deadline, and the agent HTTP client checks it again after
request preparation immediately before sending. Independent re-review and those
three regression tests pass. Already-sent requests may still complete remotely;
the runner retains the original request key and does not automatically retry.

## Boundaries

This is local/mock verification, not a live payment or wallet proof. Wallet
observations are trusted owner input with provenance. The buyer loop uses a
model callback; Backboard/provider inference integration remains separate. New
policy expiry/argument constraints are configured through Policy/Ledger APIs,
not the current HTTP policy patch or UI. Owner API access now requires a token.

MND-01 remains on its own branch pending real GOAT credentials/verification.
Qatom, live payer authorization, exact provider approval/resume, distributed run
quotas, hard process isolation and other remaining sprint issues are not completed
by this review bundle. See each subsystem document for its specific limits.
