"""MND-31 budget edits, MND-33 event paging, MND-34 401 vs 403, and CORS on server errors."""
import pytest
from fastapi.testclient import TestClient

from backend.api import main
from backend.core.ledger import Ledger
from backend.core.policy import Intent, Policy
from backend.services import orchestrator
from backend.services.payment_rail import MockRail
from backend.services.seller import MockSeller

H = {"X-Owner-Token": "owner-secret"}


@pytest.fixture
def c(monkeypatch):
    ledger = Ledger(policy=Policy(whitelisted_sellers={"seller.example": "0xa1"}, task_budget_cap=1000))
    monkeypatch.setattr(main, "ledger", ledger)
    monkeypatch.setattr(main, "rail", MockRail())
    monkeypatch.setattr(main, "seller", MockSeller())
    monkeypatch.setenv("MANDATE_OWNER_TOKEN", "owner-secret")
    client = TestClient(main.app, raise_server_exceptions=False)
    client.ledger = ledger
    return client


def spend(ledger, n=3, amount=40):
    for i in range(n):
        orchestrator.submit(ledger, main.rail, main.seller, request_key=f"k{i}",
                            intent=Intent("seller.example", "0xa1", f"item{i}", amount), tool_call={})


# MND-34
def test_missing_token_is_401_with_challenge_and_wrong_token_is_403(c):
    r = c.get("/api/v1/state")
    assert r.status_code == 401 and "OwnerToken" in r.headers["www-authenticate"]
    assert c.get("/api/v1/state", headers={"X-Owner-Token": "nope"}).status_code == 403
    assert c.get("/api/v1/state", headers=H).status_code == 200


# MND-31
def test_owner_raises_and_lowers_budget_and_ceiling(c):
    r = c.patch("/api/v1/policy", headers=H, json={"expected_version": 1, "task_budget_cap": "2.500",
                                                   "transaction_ceiling": "0.900"})
    assert r.status_code == 200, r.text
    assert (r.json()["task_budget_cap"], r.json()["transaction_ceiling"], r.json()["version"]) == (2500, 900, 2)
    spend(c.ledger)  # 120 committed
    assert c.patch("/api/v1/policy", headers=H, json={"expected_version": 2, "task_budget_cap": "0.200"}).status_code == 200
    events = [e for e in c.ledger.events() if e["type"] == "POLICY_SNAPSHOT"]
    assert events[-1]["detail"] == "Policy v3 snapshot"


def test_budget_below_committed_money_is_refused_with_the_numbers(c):
    spend(c.ledger)
    r = c.patch("/api/v1/policy", headers=H, json={"expected_version": 1, "task_budget_cap": "0.100"})
    assert r.status_code == 409 and "below the 120 already held or spent" in r.json()["detail"]


def test_stale_version_and_bad_money_are_refused(c):
    assert c.patch("/api/v1/policy", headers=H, json={"expected_version": 9, "task_budget_cap": "2"}).status_code == 409
    r = c.patch("/api/v1/policy", headers=H, json={"expected_version": 1, "task_budget_cap": "1.0001"})
    assert r.status_code == 422 and "task_budget_cap" in r.json()["detail"]
    assert c.patch("/api/v1/policy", headers=H, json={"expected_version": 1, "task_budget_cap": 2}).status_code == 422


# MND-33
def test_events_page_in_order_with_cursor_matching_stream_ids(c):
    spend(c.ledger)
    first = c.get("/api/v1/events?limit=4", headers=H).json()
    seqs = [e["seq"] for e in first["events"]]
    assert seqs == sorted(seqs) and len(seqs) == 4 and first["next"] == seqs[-1]
    rest = c.get(f"/api/v1/events?after={first['next']}&limit=500", headers=H).json()
    assert rest["events"][0]["seq"] == seqs[-1] + 1 and rest["next"] is None
    assert len(seqs) + len(rest["events"]) == len(c.ledger.events())


def test_events_filter_by_purchase_type_and_stage(c):
    spend(c.ledger)
    pid = c.ledger.purchases()[0]["id"]
    by_p = c.get(f"/api/v1/events?purchase_id={pid}", headers=H).json()["events"]
    assert by_p and all(e["purchase_id"] == pid for e in by_p)
    by_t = c.get("/api/v1/events?type=PAYMENT_SETTLED", headers=H).json()["events"]
    assert len(by_t) == 3 and {e["type"] for e in by_t} == {"PAYMENT_SETTLED"}
    by_s = c.get("/api/v1/events?stage=VERIFIED", headers=H).json()["events"]
    assert by_s and all(e["purchase_id"] for e in by_s)
    assert c.get("/api/v1/events?type=drop table", headers=H).status_code == 422


def test_events_are_owner_only(c):
    assert c.get("/api/v1/events").status_code == 401


# MND-30 follow-up
def test_server_errors_still_carry_cors_headers(c, monkeypatch):
    def boom():
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(c.ledger, "balances", boom)
    r = c.get("/api/v1/state", headers={**H, "Origin": "http://localhost:3000"})
    assert r.status_code == 500 and r.json() == {"detail": "internal error"}
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"
