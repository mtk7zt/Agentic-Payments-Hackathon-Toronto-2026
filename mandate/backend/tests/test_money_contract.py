"""Boundary and lifecycle checks for the shared integer-thousandth contract."""

import pytest

from backend.core.ledger import Ledger, LedgerError
from backend.core.policy import Intent, Policy
from backend.models.money import (
    MAX_MILLI,
    MoneyError,
    format_milli,
    milli_to_token_units,
    parse_milli,
)


@pytest.mark.parametrize("amount", [True, False, 0, -1, 0.04, 40.0, "40", None, MAX_MILLI + 1])
def test_invalid_purchase_amount_has_no_ledger_effects(amount):
    ledger = Ledger(":memory:")
    before = (ledger.balances(), ledger.purchases(), ledger.events())
    try:
        with pytest.raises(LedgerError, match="amount"):
            ledger.propose(
                request_key="invalid-amount",
                intent=Intent("seller.example", "0xa1", "item", amount),
                tool_call={},
            )
        assert (ledger.balances(), ledger.purchases(), ledger.events()) == before
        assert ledger.verify_chain()["intact"]
    finally:
        ledger.conn.close()


def test_money_safe_integer_boundaries():
    assert parse_milli("0.001") == 1
    assert parse_milli("2.000") == 2000
    assert parse_milli(format_milli(MAX_MILLI)) == MAX_MILLI
    assert milli_to_token_units(MAX_MILLI) == MAX_MILLI * 1000
    with pytest.raises(MoneyError):
        parse_milli("9007199254740.992")
    for convert in (format_milli, milli_to_token_units):
        with pytest.raises(MoneyError):
            convert(MAX_MILLI + 1)


@pytest.mark.parametrize("amount", [1, 40, MAX_MILLI])
def test_valid_integer_can_be_reserved_and_settled_exactly_once(amount):
    ledger = Ledger(":memory:", policy=Policy(
        task_budget_cap=amount, max_single_spend=amount, transaction_ceiling=amount,
        whitelisted_sellers={"seller.example": "0xa1"},
    ))
    try:
        purchase, _ = ledger.propose(
            request_key="valid-amount", intent=Intent("seller.example", "0xa1", "item", amount),
            tool_call={},
        )
        ledger.evaluate_and_reserve(purchase["id"])
        assert ledger.balances() == {"budget": amount, "available": 0, "held": amount, "settled": 0}
        assert all(type(value) is int for value in ledger.balances().values())
        ledger.claim_dispatch(purchase["id"])
        assert ledger.settle(purchase["id"], "order-1") is True
        assert ledger.settle(purchase["id"], "order-1") is False
        assert ledger.balances() == {"budget": amount, "available": 0, "held": 0, "settled": amount}
        assert ledger.verify_chain()["intact"]
    finally:
        ledger.conn.close()


def test_releasing_review_hold_restores_exact_available_allocation():
    ledger = Ledger(":memory:", policy=Policy(
        task_budget_cap=2000, whitelisted_sellers={"seller.example": "0xa1"},
    ))
    try:
        for key, amount in (("micro", 1), ("review", 1999)):
            purchase, _ = ledger.propose(
                request_key=key, intent=Intent("seller.example", "0xa1", key, amount), tool_call={},
            )
            ledger.evaluate_and_reserve(purchase["id"])
        assert ledger.balances() == {"budget": 2000, "available": 0, "held": 2000, "settled": 0}
        ledger.release(purchase["id"])
        assert ledger.balances() == {"budget": 2000, "available": 1999, "held": 1, "settled": 0}
        assert ledger.verify_chain()["intact"]
    finally:
        ledger.conn.close()
