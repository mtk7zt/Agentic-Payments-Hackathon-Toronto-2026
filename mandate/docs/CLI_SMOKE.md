# CLI smoke (MND-27)

From `mandate`, run `python scripts/smoke.py`. Default mode performs one synthetic
40-milli purchase using the mock rail, records settlement and delivery separately,
then prints stored purchase IDs, amount, stage, order reference, delivery status,
balances and evidence verification. It does not use credentials or make network
requests. Normal success is VERIFIED, intact evidence, and balanced accounting.

`python scripts/smoke.py --scenarios` runs the original five labelled mock cases:
micro purchase, approval hold, blocked redirect, failed delivery after settlement,
and rail timeout. The owner fixture explicitly reissues a key after the attack;
the agent cannot renew itself. The timeout's hold stays reserved and blind retry
is refused. This command intentionally exits 1 because UNKNOWN is never success,
even in a scenario where unknown exposure was the expected containment result.

Exit codes are 0 for a successful known outcome, 1 for unexpected/UNKNOWN outcomes,
broken evidence or inconsistent accounting, and 2 for invalid/missing CLI or live
configuration. PENDING live orders do not count as successful payment.

`--live` is explicit and restricted to the Testnet3 origin and chain 48816. It
creates at most one 40-milli order, with a unique invocation mandate and request
key (both bind the durable dispatch identity); it never
retries the POST. Printed seller data remains synthetic. No live request was made
for this PR. The existing protected GOAT adapter is used unchanged: real challenge
validation, authenticated status/receipt correlation and live settlement evidence
remain the rail owner's MND-01/MND-04 work. A normalized adapter result alone is
not independent proof of authentication or payment finality. Do not mark those
acceptance criteria complete based on mock output. No Qatom changes are included.

Verification:

```text
python -m pytest backend/tests/test_smoke_cli.py -q
python scripts/smoke.py
python scripts/smoke.py --scenarios
```

The final command is expected to exit 1; inspect its hold and retry evidence.
