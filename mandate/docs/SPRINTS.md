# Mandate · Sprint Blueprint and Definition of Done

Agentic Payments Hackathon, Toronto · 36 hours · three builders (A payments and backend, B policy and security, C UI and product).

This is the review checklist for every pull request into `main`. Each ticket lists what must be true, what goes in and comes out, and the command to run before approving. Submissions are due on BuilderBase by 10:00 ET on Sunday 15 November.

**How to use it.** Run every command from the `mandate/` folder. A command marked *to write* does not pass yet: writing that test or doing that check is part of the ticket. Statuses are from Linear as of 15 November, 13:10 ET. Linear is the source of truth; the "In the repository" lines describe the code when this file was written.

## Definition of done, for every ticket

- All three acceptance criteria are met and shown in the pull request description.
- `python -m pytest -q` passes, and `python scripts/smoke.py` exits 0.
- For frontend changes, `npm run typecheck` and `npm run build` pass in `frontend/`.
- No secret, token or real approval link is committed.
- Anything simulated is labelled as simulated in the UI and the README.
- The Linear issue is moved to In Review with the pull request linked, then to Done on merge.

## Status at a glance

| Sprint | Window | Tickets | Done | In review | Backlog |
| --- | --- | --- | --- | --- | --- |
| Sprint 1 · Rail Verification & Payment Core | Hours 0–8 | 4 | 1 | 0 | 3 |
| Sprint 2 · Mandate Engine & Attack Interception | Hours 8–20 | 8 | 6 | 0 | 2 |
| Sprint 3 · Cockpit & Live Stream | Hours 20–30 | 8 | 2 | 0 | 6 |
| Sprint 4 · Exploit Scenarios, Pitch Rehearsal & Judge PDF | Hours 30–36 | 9 | 0 | 0 | 9 |
| **Total** | | **29** | **9** | **0** | **20** |

Done since the last update: MND-05, 06, 07, 08, 09, 12, 14 and 28, all on `main` with 270 backend tests. Still open on the critical path: MND-01 (verify Qatom and GOAT for real), MND-04 (live rail adapter) and MND-10 (bind human approval to the exact purchase).


## Sprint 1 · Rail Verification & Payment Core

Hours 0–8. Prove the rails and freeze the money contract before anything is built on them.

### MND-01 · Verify rail contracts and capability gates (Qatom, GOAT Flow x402)

Linear VEN-5 · Role A · 2 h · Status: **Backlog** · Depends on: none

**In the repository:** Signing and adapter code exist. Nothing has been run against a live rail.

**Acceptance criteria**

- [ ] Record the real authentication, `catalog_search` and `agent_checkout` schemas for Qatom, with sanitized responses, in `docs/qatom-contract.md`.
- [ ] Make one signed GOAT Flow Testnet3 call and record the real create-order and order-status response fields.
- [ ] Confirm price binding, approval start and resume, receipt lookup and verification. Mark anything unsupported.

**Contract**

- In: Rail credentials in `.env` (never committed).
- Out: `docs/qatom-contract.md` and `docs/goat-flow-contract.md` with real, sanitized request and response samples.

**Verify before approving**

```bash
python -m pytest -q -k "signing or goat_flow"
python scripts/smoke.py --live   # to write
```

### MND-02 · Define purchase contracts and exact money types

Linear VEN-6 · Role B · 2 h · Status: **Done** · Depends on: none

**In the repository:** Merged to `main` (`fd90531`).

**Acceptance criteria**

- [ ] Integer thousandths internally and decimal strings over JSON. `0.001` maps to `1`, `2.000` maps to `2000`.
- [ ] Reject negative amounts, more than three decimals, unsafe integers and unknown schema fields.
- [ ] Shared TypeScript contracts live in `frontend/lib/mandate.types.ts`.

**Contract**

- In: `amount: "0.040"` (decimal string).
- Out: `amount_milli: 40` (integer). A float, a sign or a fourth decimal returns HTTP 422.

**Verify before approving**

```bash
python -m pytest -q -k "money"
```

### MND-03 · Establish isolated executor and repository baseline

Linear VEN-7 · Role A · 2 h · Status: **Backlog** · Depends on: none

**In the repository:** `.env.example`, `.gitignore` and README exist. No CI yet. Owner auth is one shared token.

**Acceptance criteria**

- [ ] Rail credentials are unavailable to the agent process and the frontend.
- [ ] Owner and agent auth scopes differ: agent routes take a session key, owner routes take the owner token.
- [ ] A mock-only CI workflow runs the tests on every pull request and can never charge money.

**Contract**

- In: A pull request.
- Out: A green check from `.github/workflows/ci.yml` running pytest, the smoke test and `next build`.

**Verify before approving**

```bash
python -m pytest -q -k "agent_needs_a_live_session_key"
git grep -nE "API_SECRET=.+|OWNER_TOKEN=.+" -- . ":!*.example" ":!docs"   # must print nothing
```

### MND-27 · Add CLI smoke test for checkout and receipt logging

Linear VEN-8 · Role A · 1 h · Status: **Backlog** · Depends on: MND-04

**In the repository:** Mock mode passes. `--live` is written but has never been run.

**Acceptance criteria**

- [ ] One command runs a mock checkout through five scenarios and prints a receipt.
- [ ] `--live` creates one real low-value order.
- [ ] Exit code is non-zero on any unexpected stage or a broken evidence chain.

**Contract**

- In: `python scripts/smoke.py [--live]`
- Out: Six `PASS` lines and a receipt with balances and `"intact": true`. Exit code 0.

**Verify before approving**

```bash
python scripts/smoke.py
```


## Sprint 2 · Mandate Engine & Attack Interception

Hours 8–20. The gateway decides, reserves and blocks. No UI is needed to prove it.

### MND-04 · Implement live and mock rail adapter

Linear VEN-9 · Role A · 3 h · Status: **Backlog** · Depends on: MND-01, MND-02, MND-03

**In the repository:** `MockRail` and `GoatFlowRail` exist. No Qatom adapter. No real purchase made.

**Acceptance criteria**

- [ ] The adapter reports exactly one of `SETTLED`, `PENDING`, `UNPAID`, `UNKNOWN`.
- [ ] A timeout or transport error returns `UNKNOWN`. Checkout is never retried automatically.
- [ ] One low-value real purchase returns a stored transaction ID and a sanitized receipt.

**Contract**

- In: `create_order(dapp_order_id, amount_milli)`
- Out: `RailResult(outcome, order_id, status, raw)`. `amount_wei` is sent as an integer string (`40` milli → `"40000"`).

**Verify before approving**

```bash
python -m pytest -q -k "goat_flow_rail or timeout"
```

### MND-05 · Publish controlled paid seller endpoints

Linear VEN-10 · Role C · 3 h · Status: **Done** · Depends on: MND-02

**In the repository:** Open on branch `feature/MND-05-controlled-sellers` (`5b6dfd0`), not yet merged.

**Acceptance criteria**

- [ ] One catalog item is bought end to end. A second uses the same schema.
- [ ] A missing or wrong bearer token returns 404 before any input is parsed.
- [ ] The input schema rejects extra fields. Responses carry the request ID, order ID and a timestamp.

**Contract**

- In: `POST /api/v1/items/{item}/fulfill` with `{request_id, rail_order_id}` and `Authorization: Bearer <seller token>`.
- Out: Item, both correlation IDs, exact price, UTC timestamp and synthetic data. 404 without the token.

**Verify before approving**

```bash
git checkout feature/MND-05-controlled-sellers && python -m pytest -q backend/tests/test_controlled_sellers.py
```

### MND-06 · Persist intents and bound the agent loop

Linear VEN-11 · Role B · 3 h · Status: **Done** · Depends on: MND-02

**In the repository:** Request-key idempotency is done. No agent loop exists; the cockpit's demo keys stand in for the agent.

**Acceptance criteria**

- [ ] A repeated `request_key` returns the prior purchase for the same body and is refused for a changed body.
- [ ] The agent's tools expose discovery, proposal and status only. Agent calls to owner routes fail.
- [ ] One task runs within explicit model-step, time and attempt limits.

**Contract**

- In: `POST /api/v1/purchases` with `X-Session-Key` and a `request_key` of 8–128 characters.
- Out: HTTP 201 with the purchase. Same key and body returns the same purchase; same key with a different body returns 409.

**Verify before approving**

```bash
python -m pytest -q -k "request_key_is_idempotent"
python -m pytest -q -k "agent_loop"   # to write
```

### MND-07 · Build deterministic mandate policy evaluator

Linear VEN-12 · Role B · 3 h · Status: **Done** · Depends on: MND-02, MND-06

**In the repository:** Done and tested in `backend/core/policy.py`. Expiry and argument constraints are not built.

**Acceptance criteria**

- [ ] Checks seller domain and address, amount, attempt ceiling and allocation, in a fixed order.
- [ ] A hard denial cannot be approved away. The reason is a stable clause plus plain language.
- [ ] Same intent, policy and allocation always return the same answer.

**Contract**

- In: `evaluate(Intent, Policy, available)`
- Out: `Evaluation(decision, clause, reason, policy_version, policy_hash)` where decision is `AUTO_APPROVED`, `HELD_FOR_REVIEW` or `BLOCKED`.

**Verify before approving**

```bash
python -m pytest -q -k "policy"
```

### MND-08 · Implement atomic task and wallet allocations

Linear VEN-13 · Role A · 3 h · Status: **Done** · Depends on: MND-04, MND-06, MND-07

**In the repository:** Done and concurrency-tested in `backend/core/ledger.py`. Reconciling against the rail's wallet balance is not built.

**Acceptance criteria**

- [ ] The hold and its event are written in one database transaction.
- [ ] Two concurrent 80-unit requests against a 100-unit budget produce one reservation and one block.
- [ ] available + held + settled = budget after every commit. Lowering the budget below held + settled is refused.

**Contract**

- In: `Ledger.evaluate_and_reserve(purchase_id)`
- Out: Stage `RESERVED`, `PENDING_APPROVAL` or `BLOCKED`, plus `BUDGET_RESERVED` in the event log when a hold is placed.

**Verify before approving**

```bash
python -m pytest -q -k "concurrent or rolls_back or review_hold"
```

### MND-09 · Implement durable state transitions and dispatch claims

Linear VEN-14 · Role A · 3 h · Status: **Done** · Depends on: MND-08

**In the repository:** Done and tested on the mock rail. A test that crashes between claim and send is not written.

**Acceptance criteria**

- [ ] A dispatch claim is committed before the rail is called. One claim per purchase.
- [ ] An uncertain send goes to recovery and keeps its hold. It is never re-sent.
- [ ] Settlement moves held to settled once. A second settlement is recorded and ignored.

**Contract**

- In: `Ledger.claim_dispatch(purchase_id)` then `Ledger.settle(purchase_id, rail_order_id)`
- Out: A claim key (also sent to the rail as `dapp_order_id`). `settle` returns `True` once, then `False`.

**Verify before approving**

```bash
python -m pytest -q -k "timeout_keeps_hold or settled_exactly_once"
```

### MND-10 · Bind human authorization to the exact purchase

Linear VEN-15 · Role B · 3 h · Status: **Backlog** · Depends on: MND-01, MND-07, MND-08

**In the repository:** Local approve, reject and expiry exist. The provider's own approval flow is not connected.

**Acceptance criteria**

- [ ] The approval summary names the seller, item, amount and policy version it applies to.
- [ ] Approval is refused when the kill-switch is on or the window has closed.
- [ ] Uses the provider's real approval and resume flow. Opening a link never counts as approval.

**Contract**

- In: `POST /api/v1/purchases/{id}/approve` or `/reject` (owner route).
- Out: Approve: the purchase continues to settlement. Reject: stage `REJECTED` and the hold is released. Late or paused: HTTP 409.

**Verify before approving**

```bash
python -m pytest -q -k "flow_through_the_api or review_hold"
```

### MND-28 · Implement agent session key lifecycle

Linear VEN-16 · Role B · 1.5 h · Status: **Done** · Depends on: MND-03, MND-14

**In the repository:** Issue and revoke are done and tested. The `frozen` state exists but nothing sets it.

**Acceptance criteria**

- [ ] A key is issued per agent. Only its SHA-256 is stored; the token is shown once.
- [ ] The kill-switch revokes every active key in the same transaction that pauses the mandate.
- [ ] A blocked out-of-mandate seller revokes the key that proposed it and records the event.

**Contract**

- In: `POST /api/v1/session-keys` (owner route).
- Out: `{key_id, token}`. After revocation the same token gets HTTP 401. `GET /api/v1/state` lists key IDs and states, never tokens.

**Verify before approving**

```bash
python -m pytest -q -k "session_key or kill_switch or injection"
```


## Sprint 3 · Cockpit & Live Stream

Hours 20–30. Recovery, evidence and the operator cockpit on the live event stream.

### MND-11 · Implement unknown-outcome reconciliation

Linear VEN-17 · Role A · 2 h · Status: **Backlog** · Depends on: MND-09, MND-10

**In the repository:** Tested on the mock rail. Lookup by `dapp_order_id` on a live rail is not built.

**Acceptance criteria**

- [ ] A timeout keeps the hold and blocks the same purchase from being resubmitted.
- [ ] Reconcile looks up the original order. Amount or balance alone never proves identity.
- [ ] Only a definitive no-payment answer releases the hold.

**Contract**

- In: `POST /api/v1/purchases/{id}/reconcile?rail_order_id=…` (owner route).
- Out: The purchase moves to `SETTLED` then `VERIFIED`, or to `EXPIRED` with the hold released, or stays in recovery.

**Verify before approving**

```bash
python -m pytest -q -k "timeout_keeps_hold"
```

### MND-12 · Separate delivery validation from payment confirmation

Linear VEN-18 · Role B · 2 h · Status: **Done** · Depends on: MND-04, MND-05, MND-09

**In the repository:** Schema, size and request-match checks are done. Freshness is not checked.

**Acceptance criteria**

- [ ] Delivery is checked only after settlement is committed, and is recorded as its own event.
- [ ] A paid-but-invalid result enters recovery. The payment stays settled and is never charged again.
- [ ] Seller text is treated as data. It cannot change authority.

**Contract**

- In: `verify_delivery(payload, item, request_id)`
- Out: `Delivery(ok, detail)`. Failure sets stage `RECOVERY_UNDELIVERED` with `delivery: FAILED`.

**Verify before approving**

```bash
python -m pytest -q -k "payment_and_delivery"
```

### MND-13 · Record hash-linked evidence and trusted checkpoints

Linear VEN-19 · Role B · 2 h · Status: **Backlog** · Depends on: MND-09

**In the repository:** Chain, append-only triggers and verifier are done. No external checkpoint, so a full rewrite is not detectable yet.

**Acceptance criteria**

- [ ] Each event stores the SHA-256 of the previous event and the policy hash in force.
- [ ] The application cannot update or delete events.
- [ ] The verifier finds the first edited, reordered or missing event. A signed checkpoint is exported outside the database.

**Contract**

- In: `GET /api/v1/evidence/verify`
- Out: `{intact: true, head, events}` or `{intact: false, first_bad_seq, events}`.

**Verify before approving**

```bash
python -m pytest -q -k "evidence_chain or integer_milliseconds"
```

### MND-14 · Enforce server-side pause and owner changes

Linear VEN-20 · Role C · 2 h · Status: **Done** · Depends on: MND-07, MND-09, MND-10

**In the repository:** Pause, key revocation and the claim check are done. Policy writes do not yet require a version match.

**Acceptance criteria**

- [ ] Pause is checked inside the dispatch-claim transaction. No new claim after the kill-switch.
- [ ] Payments already at the rail keep their hold. The UI says they may still complete.
- [ ] Resume is owner-only. Policy writes carry the version they were based on.

**Contract**

- In: `POST /api/v1/kill-switch` with `{engaged: true}` (owner route).
- Out: `{engaged, revoked: [key ids], slip: [revocation events], chain}`.

**Verify before approving**

```bash
python -m pytest -q -k "kill_switch"
```

### MND-15 · Implement semantic tokens and accessible components

Linear VEN-21 · Role C · 2 h · Status: **Backlog** · Depends on: MND-02

**In the repository:** Tokens and components are built. Contrast was calculated from token values, not measured on the running UI.

**Acceptance criteria**

- [ ] Dark slate and status tokens, sans and mono roles, 44px controls.
- [ ] Text, control and focus contrast meet WCAG 2.1 AA. Status is always a word plus a marker.
- [ ] Dialogs and the drawer trap focus, close on Escape and return focus.

**Contract**

- In: `frontend/tailwind.config.ts` and `app/globals.css`.
- Out: A build with no type errors.

**Verify before approving**

```bash
cd frontend && npm run typecheck && npm run build
```

### MND-16 · Build command center and policy controls

Linear VEN-22 · Role C · 3 h · Status: **Backlog** · Depends on: MND-08, MND-14, MND-15

**In the repository:** `CockpitHeader` and `PolicyMatrix` work in live and simulated modes.

**Acceptance criteria**

- [ ] Allocation, spent, held and available are shown separately, in thousandths.
- [ ] Every policy edit produces a new policy snapshot hash on screen.
- [ ] The kill-switch locks every control and prints the revocation slip.

**Contract**

- In: `PATCH /api/v1/policy` with any of `max_single_spend`, `strict_whitelist`, `max_attempts`, `whitelisted_sellers`.
- Out: The new policy with `version` + 1 and a new `hash`.

**Verify before approving**

```bash
cd frontend && npm run build
Manual: move the threshold slider and confirm the hash and version change
```

### MND-17 · Deliver authenticated replayable event stream

Linear VEN-23 · Role A · 2 h · Status: **Backlog** · Depends on: MND-09, MND-13

**In the repository:** `/api/events` streams and resumes. It has no authentication yet.

**Acceptance criteria**

- [ ] One server-sent event per ledger event, with the sequence number as its ID.
- [ ] A reconnect with `Last-Event-ID` resumes after that event.
- [ ] The stream requires an owner credential and is read-only.

**Contract**

- In: `GET /api/events` with optional `Last-Event-ID`.
- Out: `id: <seq>`, `event: ledger.appended`, `data: <event JSON>`. A comment line roughly every 15 seconds keeps the connection open.

**Verify before approving**

```bash
python -m pytest -q -k "api_events"
curl -N http://localhost:8000/api/events   # with the gateway running
```

### MND-18 · Build live purchase timeline and status filters

Linear VEN-24 · Role C · 2 h · Status: **Backlog** · Depends on: MND-15, MND-17

**In the repository:** `TransactionStream` renders from the gateway stream and falls back to the simulated ledger.

**Acceptance criteria**

- [ ] Each row shows intent, policy decision, ledger entries, settlement and delivery separately.
- [ ] Filters: all, auto-settled, pending review, blocked. The data source is always named.
- [ ] With the gateway offline the cockpit switches to simulated within seconds and reconnects by itself.

**Contract**

- In: `NEXT_PUBLIC_MANDATE_API_URL=http://localhost:8000`
- Out: Header shows `gateway · SSE`. Stop the gateway and it shows `gateway offline · simulated`.

**Verify before approving**

```bash
Manual: run the gateway and `npm run dev`, fire the three demo keys, then stop the gateway
```


## Sprint 4 · Exploit Scenarios, Pitch Rehearsal & Judge PDF

Hours 30–36. Prove it under attack, rehearse it, and freeze the submission.

### MND-19 · Build human escalation drawer

Linear VEN-25 · Role C · 2 h · Status: **Backlog** · Depends on: MND-10, MND-15, MND-17

**In the repository:** `EscalationDrawer` works in both modes. The countdown uses the gateway's `expires_at`.

**Acceptance criteria**

- [ ] The exact purchase and the balance after approval are shown before any action.
- [ ] Authorize and reject are disabled once the window closes or the kill-switch is on.
- [ ] Keyboard-only approve and reject work and return focus.

**Contract**

- In: A purchase in `PENDING_APPROVAL`.
- Out: Authorize → settled and delivered. Reject → hold released.

**Verify before approving**

```bash
Manual: fire demo key 2, authorize, and confirm the row reaches `SETTLED · DELIVERED`
```

### MND-20 · Build evidence dialog and sanitized export

Linear VEN-26 · Role B · 2 h · Status: **Backlog** · Depends on: MND-12, MND-13, MND-15

**In the repository:** `EvidenceModal` re-hashes the gateway's chain in the browser.

**Acceptance criteria**

- [ ] Payload, matched clause, settlement, delivery and chain integrity are separate sections.
- [ ] "Verify SHA-256 Chain" recomputes every hash in the browser, independently of the server.
- [ ] Copied JSON contains no credentials, tokens or approval links.

**Contract**

- In: `GET /api/v1/purchases/{id}/evidence`
- Out: `{schema, purchase, events, chain, redactions}`.

**Verify before approving**

```bash
Manual: open the audit pill on a blocked row and press Verify; expect `CHAIN INTACT`
```

### MND-21 · Validate accessibility and integrated happy path

Linear VEN-27 · Role B · 2 h · Status: **Backlog** · Depends on: MND-16, MND-18, MND-19, MND-20

**In the repository:** Not started. No keyboard or screen-reader pass has been done.

**Acceptance criteria**

- [ ] A keyboard and screen-reader walkthrough covers purchase, approval, recovery and evidence.
- [ ] 200% text, 320px width, contrast, reduced motion and live announcements are checked.
- [ ] One live transaction appears in the right states with no secret on screen.

**Contract**

- In: The running cockpit.
- Out: A dated checklist in `docs/ACCESSIBILITY.md` with pass or fail per item.

**Verify before approving**

```bash
Manual walkthrough; record results in docs/ACCESSIBILITY.md   # to write
```

### MND-22 · Build deterministic injection and runaway-loop scenarios

Linear VEN-28 · Role B · 1.5 h · Status: **Backlog** · Depends on: MND-07, MND-12, MND-18

**In the repository:** The injection case is tested. The runaway-loop case is not written.

**Acceptance criteria**

- [ ] An injected seller is blocked and the rail adapter records zero checkout calls.
- [ ] Over-limit and repeated cheap calls hit their hard ceilings.
- [ ] Fixtures are labelled as fixtures. The reason shown is the policy clause, not a claim to detect injection.

**Contract**

- In: Demo key 3, or `POST /api/v1/purchases` with an unlisted seller.
- Out: Stage `BLOCKED`, clause `VENDOR_NOT_WHITELISTED`, the proposing key revoked, balances unchanged.

**Verify before approving**

```bash
python -m pytest -q -k "injection or blocks_redirects"
python -m pytest -q -k "runaway"   # to write
```

### MND-23 · Run concurrency, replay, crash and recovery proof suite

Linear VEN-29 · Role A · 1.5 h · Status: **Backlog** · Depends on: MND-11, MND-13, MND-14, MND-17

**In the repository:** Race, replay, timeout and pause tests pass. Crash-injection tests are not written.

**Acceptance criteria**

- [ ] No test can produce duplicate accounting or an unauthorized dispatch.
- [ ] A crash before or after the rail call leaves the hold in place and forbids a blind retry.
- [ ] The whole suite runs in under ten seconds with no network.

**Contract**

- In: `python -m pytest -q`
- Out: All tests pass.

**Verify before approving**

```bash
python -m pytest -q
```

### MND-24 · Publish architecture, threat model and limitations

Linear VEN-30 · Role B · 1.5 h · Status: **Backlog** · Depends on: MND-22, MND-23

**In the repository:** README lists guarantees and known gaps. `docs/MANDATE_SYSTEM_GUIDE.md` covers the plain-language story. No `SECURITY.md`.

**Acceptance criteria**

- [ ] README covers quickstart, agent authority and rail assumptions.
- [ ] `SECURITY.md` explains payment-before-delivery, unknown outcomes and the non-immutable local log.
- [ ] No real approval URL, token or secret appears anywhere in the repository.

**Contract**

- In: The repository.
- Out: `SECURITY.md` and an architecture diagram in `docs/`.

**Verify before approving**

```bash
git grep -nE "(sk_live|API_SECRET=.+)" -- . ":!*.example" ":!docs"   # must print nothing
```

### MND-25 · Rehearse three-minute narrative and fallback

Linear VEN-31 · Role C · 2 h · Status: **Backlog** · Depends on: MND-21, MND-22

**In the repository:** Not started.

**Acceptance criteria**

- [ ] One run shows a paid call, a blocked attack, a human approval and the evidence view in three minutes.
- [ ] Anything simulated or recorded is labelled on screen.
- [ ] The pitch states the customer, the benefit and the limits.

**Contract**

- In: The demo script in `docs/MANDATE_SYSTEM_GUIDE.md`.
- Out: A timed run-through under three minutes, with a recorded fallback.

**Verify before approving**

```bash
Manual: timed rehearsal   # to write
```

### MND-26 · Review operational readiness and release evidence

Linear VEN-32 · Role A · 1.5 h · Status: **Backlog** · Depends on: MND-21, MND-23, MND-24

**In the repository:** Not started.

**Acceptance criteria**

- [ ] Tests, smoke test and frontend build pass from a clean clone.
- [ ] One deliberate low-value live purchase is kept as evidence and the balance reconciled.
- [ ] The submitted commit SHA and known limitations are recorded.

**Contract**

- In: A clean clone of `main`.
- Out: Green on all three commands, plus the commit SHA in the submission.

**Verify before approving**

```bash
python -m pytest -q && python scripts/smoke.py && (cd frontend && npm ci && npm run build)
```

### MND-29 · Refresh and export the judges' executive summary PDF

Linear VEN-33 · Role C · 0.5 h · Status: **Backlog** · Depends on: MND-25

**In the repository:** `docs/MANDATE_SYSTEM_GUIDE.pdf` is exported from the guide. Refresh it after the final demo is fixed.

**Acceptance criteria**

- [ ] The guide describes what was built, not what was planned.
- [ ] Live, simulated and not-built parts are labelled.
- [ ] The PDF is opened and checked after export.

**Contract**

- In: `docs/MANDATE_SYSTEM_GUIDE.md`
- Out: `docs/MANDATE_SYSTEM_GUIDE.pdf`

**Verify before approving**

```bash
python docs/export_pdf.py
```

