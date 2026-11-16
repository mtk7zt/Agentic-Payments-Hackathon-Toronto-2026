#!/usr/bin/env python3
"""Run Mandate's controlled attack scenarios and print what the gateway did.

    python -m scripts.attacks            # from mandate/
    python -m scripts.attacks --json

Every scenario is a labelled synthetic fixture on the mock rail. No network, no money.
Exit code is non-zero if any scenario lets money move where it should not.
"""
from __future__ import annotations

import argparse
import json
import sys

from backend.scenarios import ALL, FIXTURE_LABEL

EXPECT = {  # scenario -> max checkout calls allowed
    "injected-seller": 0, "address-swap": 0, "over-limit": 0, "runaway-loop": 5, "cheap-drain": 55,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    results = [s() for s in ALL]
    ok = all(r.checkout_calls <= EXPECT[r.name] and r.spent_milli <= 5_000 for r in results)
    if args.json:
        print(json.dumps({"label": FIXTURE_LABEL, "ok": ok, "scenarios": [r.as_dict() for r in results]}, indent=2))
    else:
        print(FIXTURE_LABEL)
        for r in results:
            last = next((c for c in reversed(r.clauses) if c), "-")
            print(f"  {r.name:<16} checkout calls {r.checkout_calls:>2}  spent {r.spent_milli / 1000:.3f}  "
                  f"stopped by {last}")
        print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
