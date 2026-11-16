# Qatom rail contract (MND-01 evidence)

First real purchase: 16 November 2026, 10:32 ET, item 22 "BTC Options Market Read" (0.000 USD-TDN), transaction `f4488a55-24a6-481c-9cfe-ec2ac4424a98`.

1. `agent_checkout {catalog_item_id, arguments}` returned *Not done yet* with an approval link and `Approval id: aar_…` — approval is required even at $0 unless the owner set an auto-approve limit.
2. After the owner approved, the same call with `approval_id` returned `Bought "…" for 0.000 USD-TDN … Transaction: <uuid>` plus the seller's JSON.
3. Qatom validates `arguments` against the item's schema before asking for approval.
4. Amounts are USD-TDN with three decimals (1 USD-TDN = 1000 milli in Mandate's money type).
5. There is no client idempotency key and no order lookup tool, so an unanswered call is UNKNOWN and goes to reconciliation; Mandate never retries it.

`backend/services/qatom_rail.py` implements this: PENDING with `qatom-approval:<id>` while the owner decides, SETTLED with the transaction id, UNKNOWN if the charge exceeds the reservation. Mandate's rules run first, so blocked requests make zero Qatom calls.
