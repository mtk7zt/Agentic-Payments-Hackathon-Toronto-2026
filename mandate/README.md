# Mandate

**A procurement firewall for AI agents.** A deterministic policy gateway sits between an agent's tool calls and a payment rail. The agent can only *propose* a purchase; the gateway decides, reserves the money, and records evidence.

> The model can be manipulated, but it cannot grant itself purchasing authority.

Status: hackathon prototype. Sample data and a mock rail by default. No real payment is made unless you configure GOAT Flow credentials and run it deliberately.

## Layout

```
backend/
  models/money.py          integer thousandths of USD-TDN; decimal strings in, ints inside
  core/policy.py           pure evaluate(intent, policy, available) -> decision + clause
  core/ledger.py           SQLite ledger: atomic reservations, dispatch claims, hash-linked events
  core/sessions.py         agent session keys: issue, freeze, revoke
  services/signing.py      GOAT Flow x402 HMAC-SHA256 request signing
  services/payment_rail.py PaymentRail interface, GoatFlowRail, MockRail
  services/seller.py       delivery check, separate from payment
  services/orchestrator.py propose -> reserve -> claim -> rail -> settle -> deliver
  api/main.py              FastAPI routes and the SSE event stream
  tests/                   unit, integration and crash/recovery checks
frontend/                  Next.js 14 (App Router) + Tailwind
  app/page.tsx             /        landing page with the live attack stream
  app/console/page.tsx     /console operator cockpit
  components/              CockpitHeader, PolicyMatrix, TransactionStream, EvidenceModal, ...
  lib/ledger.ts            simulated in-browser ledger the cockpit runs on today
  lib/api.ts               typed client + SSE subscribe for the backend
scripts/smoke.py           CLI smoke test (MND-27)
docs/SPRINTS.md            sprint plan
```

## Run it

```bash
# backend
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
python -m pytest                      # backend regression suite
python scripts/smoke.py               # mock rail, five scenarios, prints a receipt
uvicorn backend.api.main:app --reload # http://localhost:8000/docs

# frontend
cd frontend && npm install && npm run dev   # http://localhost:3000
```

## What the gateway guarantees

| Guarantee | Where | Test |
| --- | --- | --- |
| No float money. `0.040` is stored as `40`; one unit is 1 000 base units of a 6-decimal token. | `models/money.py` | `test_money_is_exact` |
| Two requests cannot spend the same allocation. Hold and evidence commit in one transaction. | `Ledger.evaluate_and_reserve` | `test_concurrent_reservations_cannot_overspend` |
| A dispatch claim is on disk before the rail is called. A timeout keeps the hold and can never open a second order. | `Ledger.claim_dispatch`, `orchestrator.execute` | `test_timeout_keeps_hold_and_forbids_second_order` |
| An unlisted seller, or a listed name with a different address, is blocked with zero rail calls and the proposing key is revoked. | `policy.evaluate` | `test_injection_blocks_revokes_and_leaves_wallet_untouched` |
| Payment and delivery are separate records. Paid-but-undelivered is its own recovery state with no second charge. | `orchestrator._deliver` | `test_payment_and_delivery_are_separate` |
| The kill-switch pauses the mandate and revokes every active key in one transaction. | `Ledger.engage_kill_switch` | `test_kill_switch_revokes_keys_and_stops_dispatch` |
| Events are append-only and hash-linked. An edit breaks every hash after it. | `Ledger._emit`, `verify_chain` | `test_evidence_chain_is_append_only_and_tamper_evident` |

The evidence chain is tamper-evident, not immutable: someone who can rewrite the whole database can rewrite the chain. Detecting that needs a checkpoint stored somewhere else, which is not built yet.

## Controlled synthetic seller

The optional standalone seller API serves two fixed-price synthetic items and
requires a bearer token. Set `MANDATE_SELLER_MODE=http` in the gateway to use it;
the default remains the in-process mock. See `docs/CONTROLLED_SELLERS.md` for the
two-process setup, verification command and payment-proof limitations.

## Shared money contracts

`frontend/lib/mandate.types.ts` owns the shared TypeScript contracts. Existing
imports from `lib/ledger.ts` and `lib/api.ts` continue to work through type
re-exports. The simulated cockpit types retain their UI shapes; gateway wire
types use separate names because backend recovery stages and reason codes differ.

Amounts inside the ledger are positive integer thousandths of USD-TDN, bounded
by `2**53 - 1`. `0.001` maps to `1`, and `2.000` maps to `2000`. Direct ledger
proposals reject booleans, floats (including `40.0`), zero, negatives, strings,
and unsafe integers before writing purchases or evidence. Zero remains valid
for balances and money formatting. The `Milli` TypeScript alias documents units;
it does not validate untrusted runtime values.

Purchase API amounts are decimal strings; `amount_milli` and event/balance amounts
are explicitly integer units. GOAT `amount_wei` is an integer string in token
base units: for a six-decimal token, `40` milli becomes `"40000"`. Keep provider
base units as strings in JavaScript, since they can exceed its safe integer range.

New request fingerprints bind the mandate, agent, normalized seller domain and
address, item, integer amount and complete tool-call arguments. Legacy records
replay against their stored envelope without rewriting dispatch claims.
Mock and live rails share the backend purchase contract, while a
pending live order remains `DISPATCHED` until payment confirmation. Reason-code
unions in the shared types distinguish this backend contract from the simulator.

Verify the money boundaries and exact reservation/settlement lifecycle with
`python -m pytest backend/tests/test_money_contract.py` from this directory.

The bounded buyer loop exposes discovery, proposal and status tools only, with
explicit step, time and new-request limits. See `docs/AGENT_LOOP.md` for its model
callback interface, owner authentication and timeout/replay semantics.

## Payment rail

`MANDATE_RAIL=mock` (default) uses `MockRail`. `MANDATE_RAIL=goat` uses `GoatFlowRail` against GOAT Flow x402 Testnet3; copy `.env.example` to `.env` and fill the `GOAT_FLOW_*` values.

Signing follows the published x402 API reference: body fields plus `api_key`, `timestamp` (Unix seconds) and `nonce`; empty values and `sign` removed; keys sorted; `k=v&k=v`; HMAC-SHA256, lowercase hex, sent as `X-API-Key`, `X-Timestamp`, `X-Nonce`, `X-Sign`.

Known gaps, stated plainly:

- **Not run against the live API.** The adapter is tested against a mocked transport only. The reference does not document the create-order response fields, so `GoatFlowRail._order_id` accepts the common shapes and must be checked against a real response (MND-01).
- **Order creation is not payment.** `POST /api/v1/orders` opens an order and returns payment terms. The payer-side authorization that actually moves USDC is not implemented, so a live order stays `DISPATCHED` until it is paid some other way and `reconcile` sees `PAYMENT_CONFIRMED`.
- **Qatom.** No Qatom contract is assumed anywhere. It plugs in as another `PaymentRail` once its API is confirmed.
- **The cockpit is not wired to the backend yet.** It runs on `frontend/lib/ledger.ts`, a simulated ledger that mirrors the backend's rules. `frontend/lib/api.ts` is the seam for connecting them.
- **Owner auth is a single shared token** (`MANDATE_OWNER_TOKEN`). Owner routes deny all requests when it is unset. Configure it before issuing agent sessions or using owner state, evidence, approvals or event-stream endpoints.
- Not affiliated with Y Combinator, GOAT Network or Qatom.

Wallet allocations can optionally be checked against trusted owner-recorded
snapshots. See [wallet monitoring](docs/WALLET_MONITORING.md) for the API,
provenance requirements, reconciliation workflow, and limits.
