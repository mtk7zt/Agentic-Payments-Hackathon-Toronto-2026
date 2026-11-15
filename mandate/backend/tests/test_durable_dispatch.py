"""Crash boundaries use a file ledger and independent SQLite connections."""
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from backend.core.ledger import Ledger, LedgerError
from backend.core.policy import Intent, Policy
from backend.services import orchestrator
from backend.services.payment_rail import MockRail
from backend.services.seller import MockSeller


@pytest.fixture
def store(tmp_path):
    path = str(tmp_path / "dispatch.sqlite")
    led = Ledger(path, policy=Policy(whitelisted_sellers={"seller.example": "0xa1"}))
    p, _ = led.propose(request_key="dispatch-1", intent=Intent("seller.example", "0xa1", "item", 40),
                       tool_call={})
    led.evaluate_and_reserve(p["id"])
    led.conn.close()
    return path, p["id"]


def test_crash_after_claim_before_send_retains_hold_and_enters_recovery(store):
    path, pid = store
    led = Ledger(path)
    led.claim_dispatch(pid)
    led.conn.close()  # worker disappears before calling the rail
    reopened = Ledger(path)
    rail = MockRail()
    try:
        p = orchestrator.reconcile(reopened, rail, MockSeller(), pid)
        assert p["stage"] == "RECOVERY_UNKNOWN"
        assert reopened.balances()["held"] == 40
        with pytest.raises(LedgerError):
            orchestrator.execute(reopened, rail, MockSeller(), pid)
        assert rail.create_calls == 0
        assert reopened.verify_chain()["intact"]
    finally:
        reopened.conn.close()


@pytest.mark.parametrize("send_completed", [False, True])
def test_abrupt_worker_exit_does_not_authorize_redelivery(store, tmp_path, send_completed):
    path, pid = store
    receipt = tmp_path / "external-order.txt"
    script = """
import os
import sys
from pathlib import Path
from backend.core.ledger import Ledger
from backend.services.orchestrator import execute
from backend.services.seller import MockSeller

class CrashRail:
    def create_order(self, **kwargs):
        if sys.argv[4] == 'True':
            Path(sys.argv[3]).write_text(kwargs['dapp_order_id'])
        os._exit(17)

execute(Ledger(sys.argv[1]), CrashRail(), MockSeller(), sys.argv[2])
"""
    child = subprocess.run([sys.executable, "-c", script, path, pid, str(receipt), str(send_completed)],
                           check=False, capture_output=True, text=True, timeout=20)
    assert child.returncode == 17, child.stderr
    assert receipt.exists() is send_completed
    led = Ledger(path)
    rail = MockRail()
    try:
        assert led.purchase(pid)["stage"] == "DISPATCHED"
        assert led.balances()["held"] == 40
        with pytest.raises(LedgerError):
            orchestrator.execute(led, rail, MockSeller(), pid)
        assert orchestrator.reconcile(led, rail, MockSeller(), pid)["stage"] == "RECOVERY_UNKNOWN"
        assert rail.create_calls == 0
        assert led.verify_chain()["intact"]
    finally:
        led.conn.close()


def test_independent_connections_race_for_one_claim_and_one_settlement(store):
    path, pid = store
    barrier = Barrier(2)

    def race(operation):
        led = Ledger(path)
        try:
            barrier.wait(timeout=10)
            try:
                return operation(led)
            except LedgerError:
                return "refused"
        finally:
            led.conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(race, [lambda led: led.claim_dispatch(pid)] * 2))
        assert claims.count("refused") == 1
        settlements = list(pool.map(race, [lambda led: led.settle(pid, "original-order")] * 2))
        assert sorted(settlements) == [False, True]
    led = Ledger(path)
    try:
        assert led.balances() == {"budget": 5000, "held": 0, "settled": 40, "available": 4960}
        assert [e["type"] for e in led.events()].count("DISPATCH_CLAIMED") == 1
        assert [e["type"] for e in led.events()].count("PAYMENT_SETTLED") == 1
        assert [e["type"] for e in led.events()].count("DUPLICATE_CALLBACK_IGNORED") == 1
        assert led.verify_chain()["intact"]
    finally:
        led.conn.close()


@pytest.mark.parametrize("boundary", ["DISPATCH_CLAIMED", "PAYMENT_SETTLED"])
def test_event_failure_rolls_back_claim_or_settlement_on_disk(store, boundary):
    path, pid = store
    led = Ledger(path)
    if boundary == "PAYMENT_SETTLED":
        led.claim_dispatch(pid)
    before = (led.purchase(pid), led.balances(), led.events())
    led.conn.execute(f"""CREATE TRIGGER fail_boundary BEFORE INSERT ON events
        WHEN NEW.type='{boundary}' BEGIN SELECT RAISE(ABORT, 'injected disk failure'); END""")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="injected disk failure"):
            if boundary == "DISPATCH_CLAIMED":
                led.claim_dispatch(pid)
            else:
                led.settle(pid, "original-order")
    finally:
        led.conn.close()
    reopened = Ledger(path)
    try:
        assert (reopened.purchase(pid), reopened.balances(), reopened.events()) == before
        reopened.conn.execute("DROP TRIGGER fail_boundary")
        if boundary == "DISPATCH_CLAIMED":
            reopened.claim_dispatch(pid)
        assert reopened.settle(pid, "original-order") is True
        assert reopened.balances()["settled"] == 40
        assert reopened.verify_chain()["intact"]
    finally:
        reopened.conn.close()


def test_unpaid_and_paid_terminal_states_reject_invalid_transitions(store):
    path, pid = store
    led = Ledger(path)
    try:
        with pytest.raises(LedgerError, match="no dispatch claim"):
            led.settle(pid, "unclaimed-order")
        led.claim_dispatch(pid)
        led.mark_unknown(pid, "lost response")
        with pytest.raises(LedgerError):
            led.claim_dispatch(pid)
        led.close_unpaid(pid, "confirmed cancelled")
        before = (led.purchase(pid), led.balances(), led.events())
        for transition in [lambda: led.settle(pid, "late-order"), lambda: led.claim_dispatch(pid),
                           lambda: led.close_unpaid(pid, "duplicate"),
                           lambda: led.mark_unknown(pid, "late timeout")]:
            with pytest.raises(LedgerError):
                transition()
            assert (led.purchase(pid), led.balances(), led.events()) == before
        assert led.balances()["held"] == led.balances()["settled"] == 0
    finally:
        led.conn.close()


def test_duplicate_callback_after_verified_reopen_cannot_change_paid_order(store):
    path, pid = store
    led = Ledger(path)
    orchestrator.execute(led, MockRail(), MockSeller(), pid)
    paid = led.purchase(pid)
    led.conn.close()
    reopened = Ledger(path)
    try:
        assert reopened.settle(pid, "different-callback-order") is False
        assert reopened.purchase(pid) == paid
        for transition in [lambda: reopened.claim_dispatch(pid),
                           lambda: reopened.mark_unknown(pid, "late timeout"),
                           lambda: reopened.close_unpaid(pid, "late cancellation"),
                           lambda: reopened.record_delivery(pid, False, "duplicate delivery")]:
            with pytest.raises(LedgerError):
                transition()
        assert reopened.balances()["held"] == 0
        assert reopened.balances()["settled"] == 40
        assert reopened.verify_chain()["intact"]
    finally:
        reopened.conn.close()


def test_rail_exception_after_send_is_durable_unknown_and_never_retried(store):
    path, pid = store

    class InterruptedRail(MockRail):
        def create_order(self, **kwargs):
            observer = Ledger(path)
            try:
                assert observer.purchase(pid)["stage"] == "DISPATCHED"
                assert any(e["type"] == "DISPATCH_CLAIMED" for e in observer.events())
            finally:
                observer.conn.close()
            super().create_order(**kwargs)
            raise ConnectionError("response lost after the order was created")

    rail = InterruptedRail()
    led = Ledger(path)
    try:
        p = orchestrator.execute(led, rail, MockSeller(), pid)
        assert p["stage"] == "RECOVERY_UNKNOWN"
    finally:
        led.conn.close()
    reopened = Ledger(path)
    try:
        with pytest.raises(LedgerError):
            orchestrator.execute(reopened, rail, MockSeller(), pid)
        assert rail.create_calls == 1
        assert reopened.balances()["held"] == 40
        order_id = next(iter(rail.orders.values()))["order_id"]
        p = orchestrator.reconcile(reopened, rail, MockSeller(), pid, order_id)
        assert p["stage"] == "VERIFIED"
        assert reopened.balances()["settled"] == 40
        assert rail.create_calls == 1
    finally:
        reopened.conn.close()
