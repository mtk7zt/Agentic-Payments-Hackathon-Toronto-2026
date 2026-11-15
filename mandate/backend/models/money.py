"""Exact money. Amounts are integer thousandths of USD-TDN ("milli").

No float ever touches an amount: values arrive as decimal strings, are parsed
to int once, and stay int. One milli equals 1 000 base units of a 6-decimal
token such as USDC, so conversion to the rail is an integer multiply.
"""
from __future__ import annotations

import re

MILLI_PER_UNIT = 1_000
TOKEN_DECIMALS = 6
_BASE_UNITS_PER_MILLI = 10 ** (TOKEN_DECIMALS - 3)
MAX_MILLI = 2**53 - 1  # stays exact in JSON consumers too
_DECIMAL = re.compile(r"^([0-9]{1,13})(?:\.([0-9]{1,3}))?$")


class MoneyError(ValueError):
    pass


def validate_milli(value: int, *, positive: bool = False) -> int:
    """Validate internal units before persistence or conversion; never coerce."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MoneyError("milli amount must be a non-negative int")
    if value > MAX_MILLI:
        raise MoneyError("milli amount exceeds the JSON-safe integer limit")
    if positive and value == 0:
        raise MoneyError("purchase amount must be positive")
    return value


def parse_milli(text: str) -> int:
    """'0.040' -> 40, '2.5' -> 2500. Rejects floats, signs, >3 decimals."""
    if not isinstance(text, str):
        raise MoneyError("amount must be a decimal string, not a number")
    m = _DECIMAL.match(text.strip())
    if not m:
        raise MoneyError(f"invalid amount {text!r}: up to 3 decimals, no sign")
    whole, frac = m.group(1), (m.group(2) or "").ljust(3, "0")
    value = int(whole) * MILLI_PER_UNIT + int(frac)
    return validate_milli(value)


def format_milli(value: int) -> str:
    """40 -> '0.040'."""
    validate_milli(value)
    return f"{value // MILLI_PER_UNIT}.{value % MILLI_PER_UNIT:03d}"


def milli_to_token_units(value: int) -> int:
    """Integer base units of a 6-decimal token. 40 milli -> 40000."""
    validate_milli(value)
    return value * _BASE_UNITS_PER_MILLI
