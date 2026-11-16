# Qatom rail contract (MND-01 evidence)

First real purchase: 16 November 2026, 10:32 ET, item 22 "BTC Options Market Read" (0.000 USD-TDN), transaction `f4488a55-24a6-481c-9cfe-ec2ac4424a98`.

1. `agent_checkout {catalog_item_id, arguments}` returned *Not done yet* with an approval link and `Approval id: aar_…` — approval is required even at $0 unless the owner set an auto-approve limit.
2. After the owner approved, the same call with `approval_id` returned `Bought "…" for 0.000 USD-TDN … Transaction: <uuid>` plus the seller's JSON.
3. Qatom validates `arguments` against the item's schema before asking for approval.
4. Amounts are USD-TDN with three decimals (1 USD-TDN = 1000 milli in Mandate's money type).
5. There is no client idempotency key and no order lookup tool, so an unanswered call is UNKNOWN and goes to reconciliation; Mandate never retries it.

`backend/services/qatom_rail.py` implements this: PENDING with `qatom-approval:<id>` while the owner decides, SETTLED with the transaction id, UNKNOWN if the charge exceeds the reservation. Mandate's rules run first, so blocked requests make zero Qatom calls.

The context needed to resume an approved checkout (item, arguments, reserved amount) is stored in the ledger's `rail_approvals` table, not in memory, so a gateway restart does not lose a pending approval. Each purchase records `rail`, `approval_id`, `rail_order_id` and the final `rail_tx_id`. If the context is missing, the rail answers `APPROVAL_CONTEXT_LOST` (UNKNOWN) and the hold is kept for the owner to resolve.

## Signing the backend in

Run once on the machine that runs the gateway:

```
cd mandate
python -m scripts.qatom_login            # opens Qatom in your browser; approve there
python -m scripts.qatom_login --status   # where the token is and when it expires
python -m scripts.qatom_login --refresh  # renew with the saved refresh token
```

The script follows the MCP authorization flow: it discovers Qatom's authorization server from
`QATOM_MCP_URL`, registers itself as a public client (or uses `QATOM_CLIENT_ID` if you set one),
and signs in with PKCE through a one-time `127.0.0.1` callback. The token is saved to
`QATOM_TOKEN_FILE` (default `~/.toda-mcp-token.json`) with owner-only permissions, outside the
repo. Then set `MANDATE_RAIL=qatom` and start the gateway. No money moves during sign-in.

## One real purchase through Mandate

After signing in, run one tiny purchase end to end:

```
py -m scripts.qatom_live --catalog-id 26 --amount 0.001
```

Mandate checks its rules, holds the amount and records the dispatch claim before calling Qatom.
The script prints Qatom's approval link and waits up to five minutes while you approve, then
settles once with Qatom's transaction id. The purchase ends as SETTLED: paid, with delivery
handled by Qatom rather than verified by Mandate. Amounts above 0.100 are refused. If you stop
the script, the hold stays; continue with `--resume <purchase id>`.

Qatom's sign-in may not issue a refresh token. When the saved token expires, run
`py -m scripts.qatom_login` again.
