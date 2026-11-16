# Mandate

**The spending control layer for AI agents.**

AI agents can now search, compare and check out on their own. Mandate decides whether they may. It sits between an agent and every payment rail, and turns a person's or a company's intent into rules the agent cannot talk its way around: who it may pay, how much, how often, and when a human has to say yes. Every decision is recorded as tamper-evident evidence.

> The model can be manipulated. It cannot grant itself purchasing authority.

Mandate works with any agent and any rail. Agents connect over REST today; an MCP server, so Grok, Claude, OpenAI and any MCP client can connect natively, is on the roadmap below. Rails plug in behind one contract: x402 facilitators, GOAT Flow and Qatom have adapters today, and card-network agentic tokens are next.

## Why it exists

An agent with a wallet is one prompt injection away from paying the wrong party, a runaway loop away from draining a budget, and one timeout away from paying twice. Payment networks are adding agent credentials and protocols such as AP2 and x402 standardise how agents prove authority and pay. None of them decide, for *your* household, café or fleet, what an agent is allowed to buy. Mandate is that decision layer.

## What Mandate guarantees

| Guarantee | Where it is enforced | Proof |
| --- | --- | --- |
| The agent can only *propose*. Mandate evaluates, reserves the money and records evidence before any rail is called. | `core/policy.py`, `Ledger.evaluate_and_reserve` | `test_concurrent_reservations_cannot_overspend` |
| An unlisted seller, or a trusted name with a different payment address, is blocked with zero rail calls, and the agent's key is revoked. | `policy.evaluate` | `test_attack_scenarios.py` |
| Hard ceilings per purchase, per task budget and per repeat window stop runaway loops. A weekly order is never mistaken for a loop. | `policy.evaluate`, `Ledger.propose` | `test_runaway_loop_stops_at_max_attempts`, `test_weekly_household_order_is_never_blocked_by_the_loop_guard` |
| A human approval covers the exact seller, item, arguments, amount and rules version that were shown. Any change voids it. | `Ledger.approve`, `claim_dispatch` | `test_approval_binding.py` |
| The intent to pay is on disk before the rail is called. A timeout keeps the money held and can never open a second order. | `Ledger.claim_dispatch`, `orchestrator.execute` | `test_durable_dispatch.py` |
| An unknown outcome is resolved only through the original order. Only proof that nothing was paid releases the hold, and one payment can settle only one purchase. | `orchestrator.reconcile`, `Ledger.resolve_manually` | `test_unknown_outcomes.py` |
| Payment and delivery are separate records. Paid-but-undelivered is its own recovery state with no second charge. | `orchestrator._deliver` | `test_payment_and_delivery_are_separate` |
| One switch pauses the mandate and revokes every agent key in one transaction. | `Ledger.engage_kill_switch` | `test_kill_switch_revokes_keys_and_stops_dispatch` |
| Evidence is append-only and hash-linked. Signed checkpoints detect edits, reordering and truncation. | `Ledger._emit`, `verify_chain` | `test_checkpoints.py` |
| Money is exact integers; there is no floating point anywhere in the money path. | `models/money.py` | `test_money_contract.py` |

The evidence chain is tamper-evident, not immutable: whoever controls the database and the checkpoint key could rewrite both. Production deployments should store checkpoints outside the database. [SECURITY.md](SECURITY.md) lists every limit.

## Status

Mandate is pre-release. The payment core above is complete and covered by more than 360 automated tests on every pull request. These are the remaining pieces before general availability, tracked in Linear:

| Area | Today | Before GA |
| --- | --- | --- |
| Storage | SQLite or PostgreSQL behind one interface, versioned migrations, both tested in CI; one mandate per database | Tenants (organisation and workspace) with row-level security |
| Identity | One owner token; hashed, revocable agent keys | OIDC/SSO for people, roles, per-organisation API keys, rotating agent keys |
| Agent access | REST API | MCP server, OpenAPI, Python and TypeScript SDKs, signed webhooks |
| Rails | Adapters for GOAT Flow, Qatom and a sandbox rail | Live-verified against each provider's sandbox through one shared contract suite; x402 facilitator adapter |
| Operations | Mock-only CI | OpenTelemetry, rate limits, readiness probes, deployment manifests, backups |

The sandbox rail and sample sellers exist for local development and CI. Production configuration must name a real rail.

## Architecture

```
agent ──propose──▶ Mandate gateway ──evaluate──▶ policy (pure, deterministic)
                       │                              │
                       │◀─────── decision + clause ───┘
                       ├── reserve hold + evidence (one transaction)
                       ├── human approval when required (bound to exact terms)
                       ├── durable dispatch claim ──▶ payment rail adapter ──▶ provider
                       ├── settle once / keep hold on unknown / release only on proof
                       └── delivery check, separate from payment
owner ──cockpit / API──▶ rules, approvals, kill switch, evidence, event stream
```

Details: [architecture and state machine](docs/ARCHITECTURE.md) · [security model](SECURITY.md) · [owner controls](docs/OWNER_CONTROLS.md) · [agent loop](docs/AGENT_LOOP.md) · [wallet monitoring](docs/WALLET_MONITORING.md) · [Qatom rail](docs/rails/QATOM.md)

```
backend/
  core/policy.py            pure evaluate(intent, policy, available) -> decision + clause
  core/ledger.py            reservations, dispatch claims, approvals, hash-linked evidence
  core/db.py                database interface: SQLite and PostgreSQL drivers
  core/migrations.py        versioned schema for both engines
  config.py                 settings; production refuses every sandbox shortcut
  core/sessions.py          agent session keys: issue, freeze, revoke
  services/payment_rail.py  PaymentRail contract and adapters
  services/qatom_rail.py    Qatom adapter (MCP agent checkout with owner approval)
  services/orchestrator.py  propose -> reserve -> claim -> rail -> settle -> deliver
  api/main.py               REST API and the authenticated event stream
  scenarios/                labelled attack scenarios
frontend/                   Next.js owner cockpit
scripts/                    smoke test, attack scenarios, Qatom sign-in
.github/workflows/ci.yml    sandbox-only CI on every pull request
```

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: py -m venv .venv && .venv\Scripts\activate
pip install -r backend/requirements.txt
python -m pytest                                       # full suite on SQLite
python scripts/smoke.py                                # one end-to-end purchase on the sandbox rail
python -m scripts.attacks                              # injection, address swap, over-limit, loops

export MANDATE_MODE=sandbox MANDATE_OWNER_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
uvicorn backend.api.main:app --reload                  # http://localhost:8000/docs

cd frontend && npm install
NEXT_PUBLIC_MANDATE_API_URL=http://localhost:8000 npm run dev   # http://localhost:3000/console
```

Production is the default mode. It refuses to start until every adapter is real: a persistent `DATABASE_URL`, a real rail (`goat` or `qatom`), a real seller service (`http`) and an owner token of at least 32 characters. Sandbox mode must be chosen explicitly and keeps its data in `mandate-sandbox.db`.

## Database

SQLite for local development and single-node installs; PostgreSQL for production (Supabase, RDS, Cloud SQL or self-hosted). Moving between them changes `DATABASE_URL`, not code. The schema is versioned in `backend/core/migrations.py`, and evidence tables are append-only by database trigger on both engines.

```bash
DATABASE_URL=postgresql://… python -m backend.migrate             # create or upgrade the schema
MANDATE_TEST_DATABASE_URL=postgresql://… python -m pytest          # run every test on PostgreSQL
```

CI runs the full suite on both SQLite and PostgreSQL for every pull request.

## Connecting a payment rail

| Rail | Set | Sign-in |
| --- | --- | --- |
| Sandbox (development and CI) | `MANDATE_MODE=sandbox`, `MANDATE_RAIL=mock` | none |
| GOAT Flow x402 (Testnet3) | `MANDATE_RAIL=goat` and the `GOAT_FLOW_*` values in `.env` | API key and secret from GOAT Flow; verify with `python scripts/smoke.py --live` |
| Qatom | `MANDATE_RAIL=qatom`, `QATOM_ITEM_MAP` | `python -m scripts.qatom_login` once, which saves `QATOM_TOKEN_FILE` |

Secrets live only in the gateway's environment or a secrets manager, never in the frontend or the agent.

## Owner API at a glance

`GET /api/v1/state` · `PATCH /api/v1/policy` · `GET /api/v1/purchases/{id}/approval` · `POST /api/v1/purchases/{id}/approve` · `POST /api/v1/purchases/{id}/reconcile` · `POST /api/v1/purchases/{id}/resolve` · `POST /api/v1/kill-switch` · `GET /api/v1/events` · `GET /api/v1/stream` · `POST /api/v1/evidence/checkpoints`

Agents use `POST /api/v1/purchases`, `GET /api/v1/purchases/{id}` and `GET /api/v1/catalog` with an `X-Session-Key`. The full schema is at `/docs` on a running gateway.

## License

MIT. Mandate is independent and not affiliated with GOAT Network, Qatom, xAI or any card network.
