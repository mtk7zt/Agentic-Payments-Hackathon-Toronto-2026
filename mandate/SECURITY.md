# Security model and limitations

Mandate is a hackathon prototype. It is not a production custody, anti-fraud,
refund or compliance system. The default mock rail and controlled seller data are
synthetic. Live rail capability must be observed and reconciled separately.

## Assets, actors and boundaries

Protect payment credentials, agent capabilities, owner authority, available/held/
settled allocation, original transaction identity and evidence history. Agent text
and seller responses are untrusted. The budget owner is trusted to set policy;
the executor host and its operators are trusted with credentials. Provider
availability, identity and settlement signals are external assumptions.

| Threat | Existing backend control | Remaining limit |
| --- | --- | --- |
| Malicious text proposes a different seller/address | Deterministic allowlist/address check, zero checkout on hard denial, proposing-key revocation | Does not detect every injection or protect a compromised approved seller. |
| Duplicate/replayed or altered requests | Persisted request envelope, changed-envelope rejection, unique durable dispatch claim | Provider guarantees and lost lookup context still require investigation. |
| Concurrent overspend | SQLite write transaction, exact integer money and budget invariant | Single fixture; no general multi-tenant/multi-wallet isolation guarantee. |
| Crash or timeout around send | Claim before send, UNKNOWN hold retained, no blind retry | A claimed-but-unsent request can remain unresolved until investigated. |
| Paid request returns bad data | Settlement precedes separate delivery validation | Payment cannot be undone locally; validation is not proof of data quality. |
| Stale owner edit or acknowledged pause race | Version match, transactional pause and authority rechecks | Previously committed in-flight payments can still complete. |
| Stolen agent token | Hashed storage, freeze/revoke and terminal revocation | A stolen active bearer capability can act until disabled. |
| Stolen owner token or executor credentials | Owner routes fail closed when unconfigured; separate API scopes | Owner compromise is privileged; no MFA, granular owner roles or enforced secret isolation. |
| Event modification | Append-only triggers and hash verification | Host/database administrator can bypass triggers and rewrite an uncheckpointed history. |
| External wallet movement | Optional owner-recorded observations block dispatch on known mismatch | No independent live wallet oracle or freshness guarantee. |

## Runtime and operational requirements

- Keep owner, rail and seller credentials out of the model, browser bundles,
  shared agent environments, source, logs, fixtures and exports. Commit only empty
  credential entries in .env.example. Never commit .env or provider token files.
- API capability separation is implemented; agent process/filesystem/network
  isolation is not. Do not execute untrusted code on the credential-bearing host.
- Bind development services to loopback, configure explicit permitted origins,
  and use HTTPS plus an appropriate authenticated deployment boundary outside
  local development. Shared owner bearer tokens are a prototype mechanism.
- Treat UNKNOWN as possible exposure. Preserve the hold and investigate the
  original request/order. Do not generate a new purchase key to bypass uncertainty.
- After pause/revocation, account for any already submitted payment. Resume only
  after original exposure and reported wallet discrepancies have been reconciled.
- Protect SQLite files, signing keys and backups separately. A local hash chain is
  not immutable. A trusted externally retained checkpoint is needed to detect full
  history rollback/rewrite; the local checkpoint feature remains in PR #5 review.
- Do not publish raw seller payloads, provider responses, approval links, credentials
  or wallet details as evidence. A redacted event subset may no longer be
  independently rehashable; state that limit explicitly.

Pending Qatom approval context is in memory on the current adapter; restart may
lose it. Do not infer authorization by opening a URL or infer payment from an order
ID, amount or wallet balance alone. Live identity/approval/reconciliation hardening
is owned by MND-01/MND-04/MND-10/MND-11 and was not modified by this ticket.

## Disclosure

Do not include secrets or exploitable production details in a public issue. Use
the repository's private security-reporting channel if the maintainer has enabled
one; otherwise contact the maintainer privately before publishing details. This
repository does not advertise a guaranteed response time or a bug-bounty program.

Architecture and verification entry points are in docs/ARCHITECTURE.md. Neither
passing mock tests nor this document is a production security certification.
