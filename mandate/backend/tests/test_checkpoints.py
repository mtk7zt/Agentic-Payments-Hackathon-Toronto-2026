import json
import secrets

import pytest
from fastapi.testclient import TestClient

from backend.api import main as gateway
from backend.core.ledger import Ledger, LedgerError, event_hash
from backend.core.policy import Intent

from .dbtools import drop_append_only, inject_failure


@pytest.fixture
def ledger():
    instance = Ledger(":memory:", checkpoint_key=secrets.token_urlsafe(32), checkpoint_interval=2)
    yield instance
    instance.conn.close()


def test_periodic_and_manual_checkpoints_bind_chain_without_credentials(ledger):
    ledger.issue_session_key()
    checkpoint = ledger.latest_checkpoint()
    assert checkpoint["seq"] == 2 and checkpoint["head"] == ledger.events()[-1]["hash"]
    assert checkpoint["mandate_id"] == ledger.mandate_id
    assert "signature" in checkpoint and ledger.verify_chain()["checkpoint_verified"]
    ledger.issue_session_key()
    saved = ledger.create_checkpoint()
    assert saved["seq"] == 3 and saved == ledger.latest_checkpoint()
    assert ledger.verify_chain(checkpoint=saved)["intact"]


def test_unconfigured_signing_is_explicit_and_fails_closed():
    ledger = Ledger(":memory:", checkpoint_key="")
    try:
        assert ledger.latest_checkpoint() is None
        assert ledger.verify_chain()["checkpoint_verified"] is False
        with pytest.raises(LedgerError, match="checkpoint key"):
            ledger.create_checkpoint()
    finally:
        ledger.conn.close()


def test_row_edit_reports_first_bad_link(ledger):
    ledger.issue_session_key()
    drop_append_only(ledger.conn)
    ledger.conn.execute("UPDATE events SET detail='tampered' WHERE seq=2")
    result = ledger.verify_chain()
    assert not result["intact"] and result["first_bad_seq"] == 2
    with pytest.raises(LedgerError, match="broken"):
        ledger.create_checkpoint()


def test_reordering_reports_first_broken_sequence(ledger):
    ledger.issue_session_key()
    ledger.issue_session_key()
    drop_append_only(ledger.conn)
    ledger.conn.execute("UPDATE events SET seq=99 WHERE seq=2")
    ledger.conn.execute("UPDATE events SET seq=2 WHERE seq=3")
    ledger.conn.execute("UPDATE events SET seq=3 WHERE seq=99")
    result = ledger.verify_chain()
    assert not result["intact"] and result["first_bad_seq"] == 2


def test_truncation_against_retained_checkpoint(ledger):
    ledger.issue_session_key()
    checkpoint = ledger.create_checkpoint()
    drop_append_only(ledger.conn)
    ledger.conn.execute("DELETE FROM events WHERE seq=2")
    result = ledger.verify_chain(checkpoint=checkpoint)
    assert not result["intact"] and result["first_bad_seq"] == 2
    assert result["reason"] == "checkpoint_truncated"


def test_full_rehash_cannot_forge_retained_checkpoint(ledger):
    ledger.issue_session_key()
    retained = ledger.create_checkpoint()
    drop_append_only(ledger.conn)
    previous = "0" * 64
    for event in ledger.events():
        body = {key: value for key, value in event.items() if key not in ("hash", "prev")}
        body["detail"] = "rewritten"
        digest = event_hash(previous, body)
        ledger.conn.execute("UPDATE events SET detail=?, prev=?, hash=? WHERE seq=?",
                            (body["detail"], previous, digest, event["seq"]))
        previous = digest
    result = ledger.verify_chain(checkpoint=retained)
    assert not result["intact"] and result["reason"] == "checkpoint_head_mismatch"


@pytest.mark.parametrize("field,value", [("signature", "0" * 64), ("signature", "é" * 64),
                                         ("mandate_id", "other-task"), ("agent_id", "other-agent"), ("seq", True)])
def test_forged_or_cross_scope_checkpoint_is_rejected(ledger, field, value):
    checkpoint = ledger.create_checkpoint()
    checkpoint[field] = value
    assert ledger.verify_chain(checkpoint=checkpoint)["intact"] is False


def test_checkpoint_and_event_rollback_together(ledger):
    inject_failure(ledger.conn, "fail_checkpoint", "evidence_checkpoints", "checkpoint unavailable")
    before = ledger.events()
    with pytest.raises(ledger.conn.IntegrityError):
        ledger.issue_session_key()
    assert ledger.events() == before and ledger.session_status() == []


def test_periodic_signing_refuses_rewritten_history_instead_of_blessing_it(ledger):
    ledger.issue_session_key()
    drop_append_only(ledger.conn)
    ledger.conn.execute("UPDATE events SET detail='tampered' WHERE seq=1")
    before = ledger.events()
    with pytest.raises(LedgerError, match="broken"):
        ledger.issue_session_key()  # seq 3, no checkpoint yet
        ledger.issue_session_key()  # seq 4 must not sign the altered chain
    assert ledger.events()[:2] == before and len(ledger.events()) == 3
    assert ledger.latest_checkpoint()["seq"] == 2


def test_checkpoint_append_guards_and_retained_trust_after_reopen(tmp_path):
    path = str(tmp_path / "checkpoints.db")
    key = secrets.token_urlsafe(32)
    ledger = Ledger(path, checkpoint_key=key)
    checkpoint = ledger.create_checkpoint()
    for statement in ("DELETE FROM evidence_checkpoints", "UPDATE evidence_checkpoints SET seq=0"):
        with pytest.raises(ledger.conn.IntegrityError):
            ledger.conn.execute(statement)
    ledger.conn.close()
    reopened = Ledger(path, checkpoint_key=key)
    try:
        assert reopened.verify_chain(checkpoint=checkpoint)["checkpoint_verified"]
    finally:
        reopened.conn.close()


def test_owner_checkpoint_endpoints_and_export(ledger, monkeypatch):
    secret = secrets.token_urlsafe(32)
    monkeypatch.setenv("MANDATE_OWNER_TOKEN", secret)
    monkeypatch.setattr(gateway, "ledger", ledger)
    headers = {"X-Owner-Token": secret}
    with TestClient(gateway.app) as client:
        assert client.post("/api/v1/evidence/checkpoints").status_code == 401
        response = client.post("/api/v1/evidence/checkpoints", headers=headers)
        assert response.status_code == 200
        checkpoint = response.json()
        assert client.get("/api/v1/evidence/checkpoint", headers=headers).json() == checkpoint
        verified = client.post("/api/v1/evidence/verify", headers=headers, json={"checkpoint": checkpoint})
        assert verified.json()["checkpoint_verified"]
        assert secret not in json.dumps(checkpoint)
        purchase, _ = ledger.propose(request_key="checkpoint-export", intent=Intent("seller.example", "0xa1", "item", 40), tool_call={})
        exported = client.get(f"/api/v1/purchases/{purchase['id']}/evidence", headers=headers).json()
        assert exported["checkpoint"]["ref"] == ledger.latest_checkpoint()["ref"]
        assert exported["chain"]["checkpoint_verified"]
