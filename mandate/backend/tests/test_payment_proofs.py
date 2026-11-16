"""MND-23: destructive boundaries use local SQLite and mock rails exclusively."""
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

import pytest

from backend.core.ledger import Ledger, LedgerError
from backend.core.policy import Intent, Policy
from backend.services import orchestrator
from backend.services.payment_rail import MockRail
from backend.services.seller import MockSeller

POLICY = Policy(whitelisted_sellers={"seller.example": "0xa1"}, task_budget_cap=100)
INTENT = Intent("seller.example", "0xa1", "item", 40)


def invariant(ledger, held, spent):
    balance = ledger.balances()
    assert balance["held"] == held and balance["settled"] == spent
    assert balance["available"] + held + spent == balance["budget"]
    assert ledger.verify_chain()["intact"]


@pytest.mark.parametrize("same_key", [True, False])
def test_eight_connections_race_without_duplicate_spend_or_overdraw(tmp_path, same_key):
    path = str(tmp_path / "race.db")
    ledger = Ledger(path, policy=POLICY)
    barrier = Barrier(8)
    rail = MockRail()

    def submit(index):
        worker = Ledger(path)
        try:
            barrier.wait(timeout=10)
            return orchestrator.submit(worker, rail, MockSeller(), request_key="same" if same_key else f"request-{index}",
                                       intent=INTENT, tool_call={})["id"]
        finally:
            worker.conn.close()
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(submit, range(8)))
        assert rail.create_calls == (1 if same_key else 2)
        assert len(set(ids)) == (1 if same_key else 8)
        invariant(ledger, 0, 40 if same_key else 80)
    finally:
        ledger.conn.close()


def test_changed_envelopes_racing_same_key_have_one_winner(tmp_path):
    path = str(tmp_path / "changed.db")
    ledger = Ledger(path, policy=POLICY)
    barrier = Barrier(2)
    rail = MockRail()

    def submit(index):
        worker = Ledger(path)
        try:
            barrier.wait(timeout=10)
            try:
                orchestrator.submit(worker, rail, MockSeller(), request_key="same", intent=INTENT,
                                    tool_call={"arguments": {"destination": str(index)}})
                return True
            except LedgerError:
                return False
        finally:
            worker.conn.close()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(submit, [1, 2])) == [False, True]
        assert rail.create_calls == 1
        invariant(ledger, 0, 40)
    finally:
        ledger.conn.close()


def test_twelve_settlement_callbacks_commit_only_once(tmp_path):
    path = str(tmp_path / "callbacks.db")
    ledger = Ledger(path, policy=POLICY)
    purchase, _ = ledger.propose(request_key="callback", intent=INTENT, tool_call={})
    pid = purchase["id"]
    ledger.evaluate_and_reserve(pid)
    ledger.claim_dispatch(pid)

    def settle(_):
        worker = Ledger(path)
        try:
            return worker.settle(pid, "original-mock-order")
        finally:
            worker.conn.close()
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            assert sum(pool.map(settle, range(12))) == 1
        assert len([e for e in ledger.events() if e["type"] == "DUPLICATE_CALLBACK_IGNORED"]) == 11
        invariant(ledger, 0, 40)
    finally:
        ledger.conn.close()


CRASH_WORKER = """
import os, sys
from pathlib import Path
from backend.core.ledger import Ledger
from backend.core.policy import Intent
from backend.services.payment_rail import MockRail
ledger = Ledger(sys.argv[1])
purchase, _ = ledger.propose(request_key='crash', intent=Intent('seller.example','0xa1','item',40), tool_call={})
ledger.evaluate_and_reserve(purchase['id'])
if sys.argv[2] != 'before_claim':
    claim = ledger.claim_dispatch(purchase['id'])
    if sys.argv[2] == 'after_send':
        result = MockRail().create_order(dapp_order_id=claim, amount_milli=40)
        Path(sys.argv[3]).write_text(result.order_id, encoding='utf8')
os._exit(29)
"""


@pytest.mark.parametrize("phase", ["before_claim", "after_claim", "after_send"])
def test_abrupt_worker_exit_never_authorizes_blind_resend(tmp_path, phase):
    path = str(tmp_path / "crash.db")
    journal = tmp_path / "mock-order.txt"
    ledger = Ledger(path, policy=POLICY)
    ledger.conn.close()
    result = subprocess.run([sys.executable, "-c", CRASH_WORKER, path, phase, str(journal)],
                            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True,
                            timeout=30, check=False)
    assert result.returncode == 29, result.stderr
    reopened = Ledger(path)
    rail = MockRail()
    try:
        if phase == "before_claim":
            # No durable claim and no send occurred; first dispatch remains legal.
            orchestrator.execute(reopened, rail, MockSeller(), "pur_0001")
            assert rail.create_calls == 1
            invariant(reopened, 0, 40)
        else:
            with pytest.raises(LedgerError):
                orchestrator.execute(reopened, rail, MockSeller(), "pur_0001")
            recovered = orchestrator.reconcile(reopened, rail, MockSeller(), "pur_0001")
            assert recovered["stage"] == "RECOVERY_UNKNOWN" and rail.create_calls == 0
            invariant(reopened, 40, 0)
            assert any(e["type"] == "OUTCOME_UNKNOWN" for e in reopened.events())
        assert journal.exists() == (phase == "after_send")
    finally:
        reopened.conn.close()


@pytest.mark.parametrize("control", ["pause", "wallet"])
def test_acknowledged_owner_control_blocks_another_connection(tmp_path, control):
    path = str(tmp_path / "owner.db")
    ledger = Ledger(path, policy=POLICY)
    ledger.configure_wallet(100, "synthetic opening balance")
    purchase, _ = ledger.propose(request_key="owner-control", intent=INTENT, tool_call={})
    ledger.evaluate_and_reserve(purchase["id"])
    acknowledged = Event()
    rail = MockRail()

    def owner():
        worker = Ledger(path)
        try:
            if control == "pause":
                worker.engage_kill_switch()
            else:
                worker.record_wallet_observation(99, "synthetic mismatch")
            acknowledged.set()
        finally:
            worker.conn.close()

    def dispatch():
        worker = Ledger(path)
        try:
            assert acknowledged.wait(10)
            with pytest.raises(LedgerError):
                orchestrator.execute(worker, rail, MockSeller(), purchase["id"])
        finally:
            worker.conn.close()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda fn: fn(), [owner, dispatch]))
        assert rail.create_calls == 0
        assert ledger.conn.execute("SELECT COUNT(*) FROM dispatch_claims").fetchone()[0] == 0
        invariant(ledger, 40, 0)
        assert any(e["type"] == ("AGENT_FROZEN" if control == "pause" else "WALLET_OBSERVED") for e in ledger.events())
    finally:
        ledger.conn.close()


def test_stale_local_approval_releases_only_on_explicit_expiry(tmp_path):
    ledger = Ledger(str(tmp_path / "approval.db"), policy=Policy(whitelisted_sellers={"seller.example": "0xa1"}, max_single_spend=20))
    try:
        purchase, _ = ledger.propose(request_key="stale", intent=INTENT, tool_call={})
        ledger.evaluate_and_reserve(purchase["id"], approval_window_s=-1)
        with pytest.raises(LedgerError, match="window closed"):
            ledger.approve(purchase["id"])
        invariant(ledger, 40, 0)
        ledger.release(purchase["id"], expired=True)
        invariant(ledger, 0, 0)
        assert ledger.events()[-2]["type"] == "APPROVAL_EXPIRED"
    finally:
        ledger.conn.close()
