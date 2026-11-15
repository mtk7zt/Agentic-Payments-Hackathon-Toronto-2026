# Owner-recorded wallet monitoring (MND-08)

The ledger reserves allocations and appends their evidence in one SQLite
`BEGIN IMMEDIATE` transaction. Allocation is a policy budget, not a provider
wallet balance. Available + held + settled equals budget; lowering the budget
below held + settled is rejected.

Wallet monitoring is optional and unconfigured by default, preserving the mock
workflow. It never queries GOAT or verifies a live balance. An owner supplies a
trusted, current snapshot of the wallet holding this mandate's USD-TDN funds.
Use integer thousandths throughout; booleans, floats, strings, negative amounts
and values above 2**53 - 1 are rejected. References must identify the external
source, for example an explorer block or a reconciled account statement, and
must not contain credentials.

Owner routes (all use the existing `owner()` dependency):

- `GET /api/v1/wallet` returns monitoring status and provenance.
- `POST /api/v1/wallet/configure` establishes the initial expected balance once.
- `POST /api/v1/wallet/observations` compares a current observed balance to the
  expected balance and persists the mismatch result with a hash-linked event.
- `POST /api/v1/wallet/reconcile` explicitly acknowledges explained external
  deposits or withdrawals by resetting the expected baseline, with audit.

POST body example:

```json
{"balance_milli":100,"reference":"owner: explorer block 123, wallet account reviewed"}
```

Each response labels provenance `owner_recorded`. The expected current balance
is the baseline minus ledger settlements since that baseline. Holds do not
subtract from the wallet balance. The observation response retains the snapshot
amount and the ledger settled amount at that snapshot, rather than pretending
the snapshot is live. A mismatching observation blocks every new dispatch claim
until a new matching observation or explicit owner reconciliation. Policy edits
cannot erase that mismatch. Claims also check current allocation coverage:
expected wallet funds must cover budget minus settled, including all holds.
Increasing the budget beyond that coverage blocks claims even without another
observation. These checks run inside the dispatch claim transaction and see
observations committed by another process. Existing settlement and recovery
continue to work so a mismatch never prevents recording a payment already made.

Initial configuration and baseline reconciliation refuse unresolved dispatched
or unknown payments: rebasing those would count the same debit incorrectly.
Observations remain allowed during unresolved payments; a debit observed before
a settlement callback conservatively creates a mismatch. Reconcile the original
payment first, then supply a new current observation. A failed audit append
rolls back the corresponding monitor update.

Limitations: snapshots are trusted owner input, without cryptographic provider
proof, freshness enforcement, autonomous polling, wallet identity discovery or
multi-mandate wallet coordination. Unreported external movements cannot be
detected. Use one dedicated wallet per ledger and obtain a new current snapshot
after resolving outstanding payments. Reconciliation authorizes a new baseline;
it does not move money or change ledger settlements. Monitoring cannot be
turned off through these routes once configured. MND-06 supplies the required
fail-closed owner authentication: set MANDATE_OWNER_TOKEN before using these
routes. The local integration branch includes that dependency; the isolated
wallet branch must not be deployed without it.

Verify with `python -m pytest backend/tests/test_wallet_allocations.py
backend/tests/test_wallet_api.py` from the mandate directory.
