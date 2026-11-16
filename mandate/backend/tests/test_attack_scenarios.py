"""MND-22: containment holds even when the model follows malicious text."""
from backend.core.policy import CLAUSE_TEXT, Clause
from backend.scenarios import (
    FIXTURE_LABEL,
    address_swap,
    cheap_drain,
    injected_seller,
    over_limit,
    runaway_loop,
)


def test_injected_seller_makes_zero_checkout_calls():
    o = injected_seller()
    assert o.checkout_calls == 0 and o.spent_milli == 0 and o.held_milli == 0
    assert o.stages == ["BLOCKED"] and o.clauses == ["VENDOR_NOT_WHITELISTED"]


def test_address_swap_on_a_trusted_seller_is_blocked_before_checkout():
    o = address_swap()
    assert o.checkout_calls == 0 and o.clauses == ["VENDOR_ADDRESS_MISMATCH"]


def test_over_limit_hits_the_hard_ceiling():
    o = over_limit()
    assert o.checkout_calls == 0 and o.clauses == ["ABOVE_TRANSACTION_CEILING"]


def test_runaway_loop_stops_at_max_attempts():
    o = runaway_loop(20)
    assert o.checkout_calls == 5 and o.spent_milli == 200
    assert o.stages[:5] == ["VERIFIED"] * 5
    assert set(o.clauses[5:]) == {"ATTEMPT_CEILING_REACHED"} and set(o.stages[5:]) == {"BLOCKED"}


def test_cheap_drain_stops_at_the_task_budget():
    o = cheap_drain(80)
    assert o.spent_milli <= 5_000 and o.checkout_calls == o.spent_milli // 90
    assert o.clauses[-1] == "INSUFFICIENT_ALLOCATION"


def test_fixtures_are_labelled_and_reasons_do_not_claim_detection():
    for scenario in (injected_seller, address_swap, over_limit, runaway_loop, cheap_drain):
        assert scenario().label == FIXTURE_LABEL
    for clause in (Clause.VENDOR_NOT_WHITELISTED, Clause.VENDOR_ADDRESS_MISMATCH,
                   Clause.ABOVE_TRANSACTION_CEILING, Clause.ATTEMPT_CEILING_REACHED):
        text = CLAUSE_TEXT[clause].lower()
        assert "inject" not in text and "detect" not in text and "malicious" not in text
