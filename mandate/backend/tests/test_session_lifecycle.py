"""Backend capability lifecycle; credentials never appear in owner status or evidence."""
import hashlib
import json
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from backend.api import main as gateway
from backend.core.ledger import Ledger, LedgerError
from backend.core.policy import Intent, Policy
from backend.core.sessions import SessionState
from backend.services import orchestrator
from backend.services.payment_rail import MockRail
from backend.services.seller import MockSeller
from scripts import smoke


@pytest.fixture
def ledger():
    instance = Ledger(":memory:", policy=Policy(whitelisted_sellers={"seller.example": "0xa1"}))
    yield instance
    instance.conn.close()


def propose(ledger, key, request="session-request", domain="seller.example", address="0xa1"):
    return ledger.propose(request_key=request, intent=Intent(domain, address, "item", 40),
                          tool_call={}, session_key_id=key)[0]["id"]


def test_token_is_hashed_once_and_status_is_redacted(ledger):
    key, token = ledger.issue_session_key()
    row = dict(ledger.conn.execute("SELECT * FROM session_keys").fetchone())
    assert row["token_sha256"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in json.dumps(row)
    assert ledger.session_status() == [{"key_id": key, "agent_id": ledger.agent_id, "state": "active"}]
    assert token not in json.dumps(ledger.events())
    assert row["token_sha256"] not in json.dumps(ledger.session_status())


def test_freeze_thaw_and_irreversible_revoke(ledger):
    key, token = ledger.issue_session_key()
    for state in (SessionState.FROZEN, SessionState.ACTIVE, SessionState.REVOKED):
        ledger.change_session_state(key, state)
        assert ledger.sessions.resolve(token) == (key, state)
    before = ledger.events()
    with pytest.raises(LedgerError, match="revoked"):
        ledger.change_session_state(key, SessionState.ACTIVE)
    assert ledger.events() == before
    assert [e["type"] for e in before][-3:] == ["SESSION_KEY_FROZEN", "SESSION_KEY_THAWED", "SESSION_KEY_REVOKED"]
    assert ledger.verify_chain()["intact"]


@pytest.mark.parametrize("state", [SessionState.FROZEN, SessionState.REVOKED])
@pytest.mark.parametrize("boundary", ["propose", "reserve", "claim"])
def test_inactive_session_cannot_cross_authority_boundary(ledger, state, boundary):
    key, _ = ledger.issue_session_key()
    pid = None
    if boundary != "propose":
        pid = propose(ledger, key)
    if boundary == "claim":
        ledger.evaluate_and_reserve(pid)
    ledger.sessions.set_state(key, state)
    before = (ledger.events(), ledger.balances())
    with pytest.raises(LedgerError, match="session"):
        if boundary == "propose":
            propose(ledger, key)
        elif boundary == "reserve":
            ledger.evaluate_and_reserve(pid)
        else:
            orchestrator.execute(ledger, MockRail(), MockSeller(), pid)
    assert (ledger.events(), ledger.balances()) == before
    assert ledger.conn.execute("SELECT COUNT(*) FROM dispatch_claims").fetchone()[0] == 0


def test_wrong_agent_and_unknown_key_cannot_propose(ledger):
    foreign, _ = ledger.sessions.issue("other-agent")
    for key in (foreign, "sk_missing"):
        with pytest.raises(LedgerError, match="session"):
            propose(ledger, key)
    assert ledger.purchases() == []


def test_pause_revokes_frozen_keys_and_does_not_allow_new_or_thawed_keys(ledger):
    active, _ = ledger.issue_session_key()
    frozen, token = ledger.issue_session_key()
    ledger.sessions.set_state(frozen, SessionState.FROZEN)
    assert set(ledger.engage_kill_switch()) == {active, frozen}
    assert ledger.sessions.resolve(token)[1] is SessionState.REVOKED
    with pytest.raises(LedgerError, match="paused"):
        ledger.issue_session_key()
    with pytest.raises(LedgerError):
        ledger.change_session_state(frozen, SessionState.ACTIVE)
    ledger.release_kill_switch()
    assert all(s["state"] == "revoked" for s in ledger.session_status())


def test_lifecycle_and_audit_roll_back_together(ledger):
    key, token = ledger.issue_session_key()
    before = ledger.events()
    ledger.conn.execute("CREATE TRIGGER fail_session BEFORE INSERT ON events "
                        "WHEN NEW.type='SESSION_KEY_FROZEN' BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.change_session_state(key, SessionState.FROZEN)
    assert ledger.sessions.resolve(token)[1] is SessionState.ACTIVE
    assert ledger.events() == before


def test_revocation_acknowledged_on_another_connection_prevents_dispatch(tmp_path):
    path = str(tmp_path / "sessions.db")
    ledger = Ledger(path, policy=Policy(whitelisted_sellers={"seller.example": "0xa1"}))
    key, _ = ledger.issue_session_key()
    pid = propose(ledger, key)
    ledger.evaluate_and_reserve(pid)
    acknowledged = Event()
    rail = MockRail()

    def revoke():
        owner = Ledger(path)
        try:
            owner.change_session_state(key, SessionState.REVOKED)
            acknowledged.set()
        finally:
            owner.conn.close()

    def dispatch():
        worker = Ledger(path)
        try:
            assert acknowledged.wait(5)
            with pytest.raises(LedgerError, match="session"):
                orchestrator.execute(worker, rail, MockSeller(), pid)
        finally:
            worker.conn.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda task: task(), [revoke, dispatch]))
        assert rail.create_calls == 0 and ledger.balances()["held"] == 40
        assert ledger.session_status()[0]["state"] == "revoked"
        assert ledger.verify_chain()["intact"]
    finally:
        ledger.conn.close()


def test_revocation_does_not_hide_already_dispatched_settlement(ledger):
    key, _ = ledger.issue_session_key()
    pid = propose(ledger, key)
    ledger.evaluate_and_reserve(pid)
    ledger.claim_dispatch(pid)
    ledger.change_session_state(key, SessionState.REVOKED)
    assert ledger.settle(pid, "original-order")
    assert ledger.balances()["held"] == 0 and ledger.balances()["settled"] == 40


def test_owner_approval_cannot_bypass_revoked_proposing_key(ledger):
    ledger.set_policy(max_single_spend=20)
    key, _ = ledger.issue_session_key()
    pid = propose(ledger, key)
    ledger.evaluate_and_reserve(pid)
    ledger.change_session_state(key, SessionState.REVOKED)
    before = ledger.events()
    with pytest.raises(LedgerError, match="session"):
        ledger.approve(pid)
    assert ledger.events() == before
    ledger.release(pid)
    assert ledger.balances()["held"] == 0


def test_owner_state_changes_are_scoped_validated_and_repeat_freeze_is_noop(ledger):
    foreign, _ = ledger.sessions.issue("other-agent")
    before = ledger.events()
    with pytest.raises(LedgerError, match="not found"):
        ledger.change_session_state(foreign, SessionState.REVOKED)
    key, _ = ledger.issue_session_key()
    with pytest.raises(LedgerError, match="invalid"):
        ledger.change_session_state(key, "active")
    ledger.change_session_state(key, SessionState.FROZEN)
    before = ledger.events()
    ledger.change_session_state(key, SessionState.FROZEN)
    assert ledger.events() == before
    assert all(row["agent_id"] == ledger.agent_id for row in ledger.session_status())


@pytest.mark.parametrize("domain,address", [("evil.example", "0xa1"), ("seller.example", "0xbad")])
def test_out_of_mandate_seller_revokes_only_proposing_key_without_rail_calls(ledger, domain, address):
    key, token = ledger.issue_session_key()
    other, _ = ledger.issue_session_key()
    rail = MockRail()
    purchase = orchestrator.submit(ledger, rail, MockSeller(), request_key="attack",
                                  intent=Intent(domain, address, "item", 40), tool_call={}, session_key_id=key)
    assert purchase["stage"] == "BLOCKED"
    assert rail.create_calls == 0
    assert ledger.sessions.resolve(token)[1] is SessionState.REVOKED
    assert ledger.sessions.active_key_ids(ledger.agent_id) == [other]
    assert ledger.balances()["held"] == 0
    event = ledger.events()[-1]
    assert event["type"] == "SESSION_KEY_REVOKED" and event["ref"] == key


def test_owner_http_lifecycle_and_agent_access(ledger, monkeypatch):
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("MANDATE_OWNER_TOKEN", token)
    monkeypatch.setattr(gateway, "ledger", ledger)
    headers = {"X-Owner-Token": token}
    key, agent_token = ledger.issue_session_key()
    with TestClient(gateway.app) as client:
        path = f"/api/v1/session-keys/{key}/state"
        assert client.get("/api/v1/session-keys").status_code == 403
        assert client.post(path, json={"state": "frozen"}).status_code == 403
        assert client.post(path, headers={"X-Session-Key": agent_token}, json={"state": "frozen"}).status_code == 403
        assert client.post(path, headers=headers, json={"state": "frozen"}).status_code == 200
        assert client.get("/api/v1/catalog", headers={"X-Session-Key": agent_token}).status_code == 401
        assert client.post(path, headers=headers, json={"state": "active"}).status_code == 200
        assert client.get("/api/v1/catalog", headers={"X-Session-Key": agent_token}).status_code == 200
        assert client.post(path, headers=headers, json={"state": "revoked"}).status_code == 200
        assert client.post(path, headers=headers, json={"state": "active"}).status_code == 409
        assert client.post(path, headers=headers, json={"state": "invalid"}).status_code == 422
        status = client.get("/api/v1/session-keys", headers=headers)
        assert status.status_code == 200 and status.json()[0]["state"] == "revoked"
        assert agent_token not in status.text and "token_sha256" not in status.text
        monkeypatch.delenv("MANDATE_OWNER_TOKEN")
        assert client.get("/api/v1/session-keys", headers=headers).status_code == 403
        assert client.post(path, headers=headers, json={"state": "frozen"}).status_code == 403


def test_offline_smoke_explicitly_reissues_key_after_attack(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["smoke.py", "--scenarios"])
    assert smoke.main() == 1  # Intentional UNKNOWN still proves containment but cannot signal success.
    output = capsys.readouterr().out
    assert "MOCK owner issued a fresh session key" in output
    assert "RECOVERY_UNKNOWN" in output and '"intact": true' in output
