import hashlib
import hmac
import sqlite3
import threading

import pytest

from backend.core.ledger import Ledger, LedgerError, Stage
from backend.core.policy import Clause, Decision, Intent, Policy, evaluate
from backend.models.money import (
    MoneyError,
    format_milli,
    milli_to_token_units,
    parse_milli,
)
from backend.services import orchestrator
from backend.services.payment_rail import (
    GoatFlowConfig,
    GoatFlowRail,
    MockRail,
    RailOutcome,
)
from backend.services.seller import MockSeller
from backend.services.signing import sign, signing_string

A = ("seller-a.example", "0xa1")
POLICY = Policy(whitelisted_sellers={A[0]: A[1]})


def intent(amount=40, dom=A[0], addr=A[1], attempt=1):
    return Intent(dom, addr, "item", amount, attempt)


# ── money ──
def test_money_is_exact():
    assert parse_milli("0.040") == 40 and parse_milli("2.5") == 2500 and parse_milli("2") == 2000
    assert format_milli(1800) == "1.800"
    assert milli_to_token_units(40) == 40_000  # 0.04 of a 6-decimal token
    assert sum(parse_milli("0.001") for _ in range(1000)) == parse_milli("1.000")


@pytest.mark.parametrize("bad", ["-1", "0.0001", "1e3", "abc", "", " ", "1.", ".5"])
def test_money_rejects(bad):
    with pytest.raises(MoneyError):
        parse_milli(bad)


def test_money_rejects_floats():
    with pytest.raises(MoneyError):
        parse_milli(0.04)  # type: ignore[arg-type]


# ── policy ──
def test_policy_decisions():
    assert evaluate(intent(100), POLICY, 5000).decision is Decision.AUTO_APPROVED
    assert evaluate(intent(101), POLICY, 5000).clause is Clause.ABOVE_AUTO_APPROVE_LIMIT
    assert evaluate(intent(3001), POLICY, 5000).clause is Clause.ABOVE_TRANSACTION_CEILING
    assert evaluate(intent(40, attempt=6), POLICY, 5000).clause is Clause.ATTEMPT_CEILING_REACHED
    assert evaluate(intent(40), POLICY, 39).clause is Clause.INSUFFICIENT_ALLOCATION


def test_policy_blocks_redirects():
    assert evaluate(intent(dom="attacker.eth", addr="0xdead"), POLICY, 5000).clause is Clause.VENDOR_NOT_WHITELISTED
    # right name, wrong address: still a hard block, never a review
    ev = evaluate(intent(addr="0xdead"), POLICY, 5000)
    assert ev.decision is Decision.BLOCKED and ev.clause is Clause.VENDOR_ADDRESS_MISMATCH


def test_policy_is_deterministic_and_hash_tracks_changes():
    assert evaluate(intent(), POLICY, 5000) == evaluate(intent(), POLICY, 5000)
    assert POLICY.hash() != Policy(whitelisted_sellers={A[0]: A[1]}, max_single_spend=110).hash()


# ── ledger ──
def make(**kw):
    led = Ledger(":memory:", policy=Policy(whitelisted_sellers={A[0]: A[1]}, **kw))
    return led, led.issue_session_key()[0]


def test_concurrent_reservations_cannot_overspend():
    led, _key = make(task_budget_cap=100, max_single_spend=100)
    ids = [led.propose(request_key=f"req-{i}-xxxx", intent=intent(80), tool_call={})[0]["id"] for i in range(2)]
    threads = [threading.Thread(target=led.evaluate_and_reserve, args=(i,)) for i in ids]
    [t.start() for t in threads]
    [t.join() for t in threads]
    stages = sorted(led.purchase(i)["stage"] for i in ids)
    assert stages == ["BLOCKED", "RESERVED"]
    b = led.balances()
    assert b["held"] == 80 and b["available"] + b["held"] + b["settled"] == b["budget"] == 100


def test_request_key_is_idempotent():
    led, _key = make()
    a, created = led.propose(request_key="same-key-1", intent=intent(40), tool_call={})
    b, again = led.propose(request_key="same-key-1", intent=intent(40), tool_call={})
    assert created and not again and a["id"] == b["id"]
    with pytest.raises(LedgerError):
        led.propose(request_key="same-key-1", intent=intent(41), tool_call={})


def test_timeout_keeps_hold_and_forbids_second_order():
    led, _key = make()
    rail = MockRail({"pur_0001": RailOutcome.UNKNOWN})
    p = orchestrator.submit(led, rail, MockSeller(), request_key="timeout-key", intent=intent(80), tool_call={})
    assert p["stage"] == Stage.RECOVERY_UNKNOWN.value and led.balances()["held"] == 80
    with pytest.raises(LedgerError):
        orchestrator.execute(led, rail, MockSeller(), p["id"])
    assert rail.create_calls == 1
    # reconcile against the ORIGINAL order settles exactly once, even if called twice
    order_id = rail.find_by_dapp_order_id(next(iter(rail.orders)))
    orchestrator.reconcile(led, rail, MockSeller(), p["id"], order_id)
    assert led.settle(p["id"], order_id) is False
    b = led.balances()
    assert b["held"] == 0 and b["settled"] == 80 and rail.create_calls == 1


def test_payment_and_delivery_are_separate():
    led, _key = make()
    seller = MockSeller(fail_items={"item"})
    p = orchestrator.submit(led, MockRail(), seller, request_key="deliver-key", intent=intent(60), tool_call={})
    assert p["stage"] == Stage.RECOVERY_UNDELIVERED.value and p["delivery"] == "FAILED"
    assert led.balances()["settled"] == 60  # payment stands
    p = orchestrator.reconcile(led, MockRail(), MockSeller(), p["id"])
    assert p["stage"] == Stage.VERIFIED.value and led.balances()["settled"] == 60  # no second charge


def test_injection_blocks_revokes_and_leaves_wallet_untouched():
    led, key = make()
    rail = MockRail()
    p = orchestrator.submit(led, rail, MockSeller(), request_key="attack-key1",
                            intent=intent(2500, "attacker.eth", "0xdead"), tool_call={}, session_key_id=key)
    assert p["stage"] == "BLOCKED" and p["clause"] == "VENDOR_NOT_WHITELISTED"
    assert rail.create_calls == 0 and led.balances()["settled"] == 0 and led.balances()["held"] == 0
    assert led.sessions.active_key_ids("agent04") == []
    assert any(e["type"] == "SESSION_KEY_REVOKED" for e in led.events())


def test_kill_switch_revokes_keys_and_stops_dispatch():
    led, key = make()
    p, _ = led.propose(request_key="kill-key-01", intent=intent(40), tool_call={})
    led.evaluate_and_reserve(p["id"])
    assert led.engage_kill_switch() == [key]
    with pytest.raises(LedgerError):
        led.claim_dispatch(p["id"])
    q, _ = led.propose(request_key="kill-key-02", intent=intent(40), tool_call={})
    assert led.evaluate_and_reserve(q["id"]).clause is Clause.AGENT_FROZEN


def test_evidence_chain_is_append_only_and_tamper_evident():
    led, _key = make()
    orchestrator.submit(led, MockRail(), MockSeller(), request_key="chain-key1", intent=intent(40), tool_call={})
    assert led.verify_chain()["intact"]
    with pytest.raises(sqlite3.DatabaseError):
        led.conn.execute("UPDATE events SET detail='edited' WHERE seq=2")
    with pytest.raises(sqlite3.DatabaseError):
        led.conn.execute("DELETE FROM events WHERE seq=2")
    # someone with schema access drops the guard and edits history: the hashes give it away
    led.conn.execute("DROP TRIGGER events_no_update")
    led.conn.execute("UPDATE events SET detail='edited' WHERE seq=2")
    broken = led.verify_chain()
    assert broken["intact"] is False and broken["first_bad_seq"] == 2


def test_failed_transaction_rolls_back_completely():
    led, _key = make()
    before = (led.balances(), len(led.events()))
    with pytest.raises(LedgerError):
        led.set_policy(task_budget_cap=-1)
    assert (led.balances(), len(led.events())) == before


# ── GOAT Flow signing ──
def test_signing_string_follows_the_published_rules():
    s = signing_string({"dapp_order_id": "o1", "chain_id": 48816, "amount_wei": "40000", "memo": "", "ok": False,
                        "sign": "ignored"}, "KEY", "1700000000", "nonce-1234")
    assert s == ("amount_wei=40000&api_key=KEY&chain_id=48816&dapp_order_id=o1&nonce=nonce-1234&ok=false"
                 "&timestamp=1700000000")


def test_sign_is_hmac_sha256_lower_hex():
    h = sign({"a": "1"}, "KEY", "SECRET", timestamp="1700000000", nonce="nonce-1234")
    want = hmac.new(b"SECRET", b"a=1&api_key=KEY&nonce=nonce-1234&timestamp=1700000000", hashlib.sha256).hexdigest()
    assert h == {"X-API-Key": "KEY", "X-Timestamp": "1700000000", "X-Nonce": "nonce-1234", "X-Sign": want}
    assert sign({"a": "1"}, "KEY", "SECRET")["X-Nonce"] != sign({"a": "1"}, "KEY", "SECRET")["X-Nonce"]


def test_signing_refuses_nested_values():
    with pytest.raises(TypeError):
        signing_string({"items": [1, 2]}, "K", "1", "nonce-1234")  # type: ignore[dict-item]


def test_goat_flow_rail_request_shape_and_status_mapping():
    import httpx

    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["headers"], seen["body"] = str(req.url), req.headers, req.content
        if req.method == "POST":
            return httpx.Response(402, json={"order_id": "ord_1", "status": "CHECKOUT_VERIFIED"})
        return httpx.Response(200, json={"status": "PAYMENT_CONFIRMED"})

    cfg = GoatFlowConfig(api_key="K", api_secret="S", from_address="0xabc")
    rail = GoatFlowRail(cfg, httpx.Client(base_url=cfg.api_url, transport=httpx.MockTransport(handler)))
    res = rail.create_order(dapp_order_id="mnd-1", amount_milli=40)
    assert res.outcome is RailOutcome.PENDING and res.order_id == "ord_1"
    assert seen["url"] == "https://flow-api.testnet3.goat.network/api/v1/orders"
    assert b'"amount_wei":"40000"' in seen["body"] and b'"chain_id":48816' in seen["body"]
    assert {"x-api-key", "x-timestamp", "x-nonce", "x-sign"} <= set(seen["headers"].keys())
    assert rail.get_order("ord_1").outcome is RailOutcome.SETTLED

    def boom(req):
        raise httpx.ReadTimeout("slow")

    rail = GoatFlowRail(cfg, httpx.Client(base_url=cfg.api_url, transport=httpx.MockTransport(boom)))
    assert rail.create_order(dapp_order_id="mnd-2", amount_milli=40).outcome is RailOutcome.UNKNOWN
