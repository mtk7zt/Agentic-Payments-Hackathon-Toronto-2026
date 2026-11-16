# Mock payment proof matrix (MND-23)

Run `python -m pytest backend/tests/test_payment_proofs.py -q` from `mandate`.
All destructive cases use SQLite and MockRail exclusively; no provider, approval
URL, wallet credential or network connection is involved.

| Boundary | Observable proof |
| --- | --- |
| Eight connections replaying one envelope | One purchase, one rail call, 40 milli settled once. |
| Eight distinct purchases against 100 milli | At most two 40-milli purchases settle; no overdraw. |
| Same key with different arguments | Exactly one envelope wins; the other is refused. |
| Twelve duplicate settlement callbacks | One settlement, eleven recorded ignored callbacks. |
| Abrupt process exit before claim | No send occurred; the first claim can proceed once. |
| Exit after claim but before send | Reopen retains exposure as UNKNOWN; no automatic resend. |
| Exit after mock send before local order storage | Mock journal proves send; reopen does not send again. |
| Acknowledged pause on another connection | No later claim and zero rail calls; event records pause. |
| Acknowledged wallet mismatch | No later claim; held money and mismatch evidence retained. |
| Expired local approval | Approval is refused; only explicit expiry releases the hold. |

Every case checks exact integer-thousandth balances and chain integrity. The
existing dispatch, owner-control, wallet, intent and session suites provide further
boundary coverage. Process-crash fixtures exit with os._exit rather than relying
on a Python exception or clean connection close.

These are deterministic local proofs, not provider guarantees, isolation proofs,
or evidence that every process scheduling order is safe. Provider approval/resume
binding remains protected MND-10 work. Signed checkpoint and browser SSE proofs
live in their separate MND-13/MND-17 PRs and are not duplicated or claimed merged
here. Re-run the complete suite after those branches land.
