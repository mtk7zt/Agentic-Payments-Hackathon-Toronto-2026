"""MND-10: an owner approval covers exactly the terms they saw and nothing else."""
import pytest

from backend.core.ledger import Ledger, LedgerError, Stage
from backend.core.policy import Intent, Policy

SELLERS = {"seller.example": "0xa1", "other.example": "0xb2"}
# max_single_spend 30 thousandths: a 40 purchase is held for review instead of auto-approved.
POLICY = Policy(whitelisted_sellers=SELLERS, task_budget_cap=1000, max_single_spend=30)


def held(ledger: Ledger, key: str, seller="seller.example", addr="0xa1", amount=40, args=None) -> str:
    p, _ = ledger.propose(request_key=key, intent=Intent(seller, addr, "item", amount),
                          tool_call={"arguments": args or {"q": "oat milk"}})
    ledger.evaluate_and_reserve(p["id"])
    assert ledger.purchase(p["id"])["stage"] == Stage.PENDING_APPROVAL.value
    return p["id"]


def test_summary_lists_the_exact_terms_and_a_digest():
    ledger = Ledger(policy=POLICY)
    s = ledger.approval_summary(held(ledger, "a"))
    assert (s["seller_domain"], s["seller_address"], s["item"], s["amount_milli"]) == ("seller.example", "0xa1", "item", 40)
    assert s["policy_version"] == 1 and len(s["digest"]) == 64 and len(s["arguments_sha256"]) == 64


def test_digest_for_one_purchase_cannot_approve_another():
    ledger = Ledger(policy=POLICY)
    a, b = held(ledger, "a"), held(ledger, "b", seller="other.example", addr="0xb2")
    digest_a = ledger.approval_summary(a)["digest"]
    with pytest.raises(LedgerError, match="does not match"):
        ledger.approve(b, digest_a)
    ledger.approve(a, digest_a)
    assert ledger.purchase(a)["stage"] == Stage.RESERVED.value


def test_identical_terms_still_get_distinct_digests():
    ledger = Ledger(policy=POLICY)
    a, b = held(ledger, "a"), held(ledger, "b")
    assert ledger.approval_summary(a)["digest"] != ledger.approval_summary(b)["digest"]


def test_purchase_edited_after_request_invalidates_the_approval():
    ledger = Ledger(policy=POLICY)
    pid = held(ledger, "a")
    digest = ledger.approval_summary(pid)["digest"]
    ledger.conn.execute("UPDATE purchases SET tool_call_json=? WHERE id=?", ('{"arguments":{"q":"gift cards"}}', pid))
    with pytest.raises(LedgerError, match="changed after approval"):
        ledger.approve(pid, digest)


def test_policy_change_before_approval_invalidates_it():
    ledger = Ledger(policy=POLICY)
    pid = held(ledger, "a")
    digest = ledger.approval_summary(pid)["digest"]
    ledger.set_policy(expected_version=1, max_attempts=3)
    with pytest.raises(LedgerError, match="policy v1; policy is now v2"):
        ledger.approve(pid, digest)


def test_policy_change_after_approval_stops_dispatch():
    ledger = Ledger(policy=POLICY)
    pid = held(ledger, "a")
    ledger.approve(pid, ledger.approval_summary(pid)["digest"])
    ledger.set_policy(expected_version=1, max_attempts=3)
    with pytest.raises(LedgerError, match="policy v1; policy is now v2"):
        ledger.claim_dispatch(pid)
    assert ledger.balances()["held"] == 40  # hold kept; nothing reached the rail


def test_approval_is_recorded_with_its_digest_in_the_evidence_chain():
    ledger = Ledger(policy=POLICY)
    pid = held(ledger, "a")
    digest = ledger.approval_summary(pid)["digest"]
    ledger.approve(pid, digest)
    granted = [e for e in ledger.events() if e["type"] == "APPROVAL_GRANTED"]
    assert granted[-1]["ref"] == digest and "policy v1" in granted[-1]["detail"]
    assert ledger.verify_chain()["intact"]
