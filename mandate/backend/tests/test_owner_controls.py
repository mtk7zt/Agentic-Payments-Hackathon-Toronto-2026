"""MND-14: stale owner edits and unreconciled resume must fail atomically."""

import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from fastapi.testclient import TestClient

from backend.api import main as gateway
from backend.core.ledger import Ledger, LedgerError
from backend.core.policy import Intent, Policy
from backend.services.payment_rail import MockRail
from backend.services.seller import MockSeller

POLICY = Policy(whitelisted_sellers={"seller.example": "0xa1"})


def reserve(ledger, key="owner-control-request"):
    purchase, _ = ledger.propose(
        request_key=key, intent=Intent("seller.example", "0xa1", "item", 40), tool_call={},
    )
    ledger.evaluate_and_reserve(purchase["id"])
    return purchase["id"]


def test_stale_policy_version_cannot_overwrite_or_append_evidence():
    ledger = Ledger(":memory:", policy=POLICY)
    try:
        version = ledger.policy().version
        updated = ledger.set_policy(expected_version=version, max_single_spend=80)
        assert updated.version == version + 1 and updated.max_single_spend == 80
        before = (ledger.policy(), ledger.events())
        with pytest.raises(LedgerError, match="version"):
            ledger.set_policy(expected_version=version, max_single_spend=90)
        assert (ledger.policy(), ledger.events()) == before
    finally:
        ledger.conn.close()


@pytest.mark.parametrize("version", [True, 0, -1, "1", 1.0])
def test_explicit_ledger_version_rejects_invalid_types_without_writes(version):
    ledger = Ledger(":memory:", policy=POLICY)
    try:
        before = (ledger.policy(), ledger.events())
        with pytest.raises(LedgerError, match="version"):
            ledger.set_policy(expected_version=version, max_single_spend=80)
        assert (ledger.policy(), ledger.events()) == before
    finally:
        ledger.conn.close()


def test_policy_version_and_values_roll_back_if_audit_append_fails():
    ledger = Ledger(":memory:", policy=POLICY)
    try:
        before = (ledger.policy(), ledger.events())
        ledger.conn.execute(
            "CREATE TRIGGER fail_policy BEFORE INSERT ON events WHEN NEW.type='POLICY_SNAPSHOT' "
            "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END",
        )
        with pytest.raises(sqlite3.IntegrityError):
            ledger.set_policy(expected_version=ledger.policy().version, max_single_spend=80)
        assert (ledger.policy(), ledger.events()) == before
    finally:
        ledger.conn.close()


def test_independent_owner_writes_with_same_version_have_one_winner(tmp_path):
    path = str(tmp_path / "policy.db")
    ledger = Ledger(path, policy=POLICY)
    version = ledger.policy().version
    ready = Barrier(2)

    def write(threshold):
        writer = Ledger(path)
        try:
            ready.wait(timeout=5)
            try:
                writer.set_policy(expected_version=version, max_single_spend=threshold)
                return True
            except LedgerError:
                return False
        finally:
            writer.conn.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(write, [80, 90])) == [False, True]
        assert ledger.policy().version == version + 1
        assert len([event for event in ledger.events() if event["type"] == "POLICY_SNAPSHOT"]) == 2
        assert ledger.verify_chain()["intact"]
    finally:
        ledger.conn.close()


@pytest.mark.parametrize("unknown", [False, True])
def test_resume_refuses_unresolved_dispatch_without_changing_pause(unknown):
    ledger = Ledger(":memory:", policy=POLICY)
    try:
        key, _ = ledger.issue_session_key()
        pid = reserve(ledger)
        ledger.claim_dispatch(pid)
        if unknown:
            ledger.mark_unknown(pid, "response lost")
        ledger.engage_kill_switch()
        before = (ledger.policy(), ledger.events(), ledger.balances())
        with pytest.raises(LedgerError, match="unresolved"):
            ledger.release_kill_switch()
        assert (ledger.policy(), ledger.events(), ledger.balances()) == before
        assert key not in ledger.sessions.active_key_ids(ledger.agent_id)
        ledger.settle(pid, "original-order")  # Pause cannot suppress accounting for an already-made payment.
        ledger.release_kill_switch()
        assert ledger.policy().paused is False and ledger.balances()["settled"] == 40
        assert ledger.sessions.active_key_ids(ledger.agent_id) == []
    finally:
        ledger.conn.close()


def test_wallet_mismatch_blocks_resume_until_matching_observation():
    ledger = Ledger(":memory:", policy=POLICY)
    try:
        ledger.configure_wallet(5000, "owner: opening snapshot")
        ledger.engage_kill_switch()
        ledger.record_wallet_observation(4999, "owner: mismatch")
        before = ledger.events()
        with pytest.raises(LedgerError, match="wallet"):
            ledger.release_kill_switch()
        assert ledger.policy().paused and ledger.events() == before
        ledger.record_wallet_observation(5000, "owner: corrected snapshot")
        ledger.release_kill_switch()
        before = (ledger.policy(), ledger.events())
        ledger.release_kill_switch()
        assert (ledger.policy(), ledger.events()) == before
    finally:
        ledger.conn.close()


def test_no_new_claim_after_pause_acknowledgement(tmp_path):
    path = str(tmp_path / "pause.db")
    ledger = Ledger(path, policy=POLICY)
    pid = reserve(ledger)
    acknowledged = Event()

    def pause():
        owner = Ledger(path)
        try:
            owner.engage_kill_switch()
            acknowledged.set()
        finally:
            owner.conn.close()

    def dispatch():
        worker = Ledger(path)
        try:
            assert acknowledged.wait(5)
            with pytest.raises(LedgerError, match="kill-switch"):
                worker.claim_dispatch(pid)
        finally:
            worker.conn.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda task: task(), [pause, dispatch]))
        assert ledger.conn.execute("SELECT COUNT(*) FROM dispatch_claims").fetchone()[0] == 0
        assert ledger.balances()["held"] == 40 and ledger.verify_chain()["intact"]
    finally:
        ledger.conn.close()


@pytest.fixture
def api(monkeypatch):
    ledger = Ledger(":memory:", policy=POLICY)
    owner_token = secrets.token_urlsafe(32)
    monkeypatch.setenv("MANDATE_OWNER_TOKEN", owner_token)
    monkeypatch.setattr(gateway, "ledger", ledger)
    monkeypatch.setattr(gateway, "rail", MockRail())
    monkeypatch.setattr(gateway, "seller", MockSeller())
    with TestClient(gateway.app) as client:
        yield client, ledger, {"X-Owner-Token": owner_token}
    ledger.conn.close()


def test_policy_http_requires_version_and_returns_409_for_stale_edit(api):
    client, ledger, headers = api
    assert client.patch("/api/v1/policy", json={"max_single_spend": "0.080"}, headers=headers).status_code == 422
    version = ledger.policy().version
    body = {"expected_version": version, "max_single_spend": "0.080"}
    response = client.patch("/api/v1/policy", json=body, headers=headers)
    assert response.status_code == 200 and response.json()["max_single_spend"] == 80
    assert response.json()["version"] == version + 1
    before = ledger.events()
    assert client.patch("/api/v1/policy", json=body, headers=headers).status_code == 409
    assert ledger.events() == before


@pytest.mark.parametrize("version", [True, "1", 1.0, 0, -1, None])
def test_http_policy_version_is_strict_integer(api, version):
    client, ledger, headers = api
    before = ledger.events()
    assert client.patch("/api/v1/policy", json={"expected_version": version}, headers=headers).status_code == 422
    assert ledger.events() == before


def test_owner_resume_api_requires_reconciled_state_and_pause_notice(api):
    client, ledger, headers = api
    pid = reserve(ledger)
    ledger.claim_dispatch(pid)
    paused = client.post("/api/v1/kill-switch", json={"engaged": True}, headers=headers)
    assert paused.status_code == 200 and "may still complete" in paused.json()["notice"]
    assert client.post("/api/v1/kill-switch", json={"engaged": False}).status_code == 403
    assert client.post("/api/v1/kill-switch", json={"engaged": False}, headers=headers).status_code == 409
    ledger.close_unpaid(pid, "operator proof: no payment was sent")
    assert client.post("/api/v1/kill-switch", json={"engaged": False}, headers=headers).json() == {"engaged": False}
