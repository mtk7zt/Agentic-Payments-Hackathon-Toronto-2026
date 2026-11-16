"""MND-32: every purchase records its rail and ids, and a pending rail approval survives a restart."""
import sqlite3

from backend.core.ledger import Ledger, Stage
from backend.core.policy import Intent, Policy
from backend.services import orchestrator
from backend.services.payment_rail import MockRail, RailOutcome
from backend.services.qatom_rail import APPROVAL_PREFIX, QatomConfig, QatomRail
from backend.services.seller import Seller

from .test_qatom_rail import APPROVAL, BOUGHT, FakeMcp

AID = "aar_37b275ee-ef60-47f0-b9c2-bcdd2051f858"
TX = "f4488a55-24a6-481c-9cfe-ec2ac4424a98"
POLICY = Policy(task_budget_cap=5000, max_single_spend=1000, whitelisted_sellers={"seller.example": "0xa1"})


def _reserved(ledger: Ledger, key: str = "k1", item: str = "btc_read") -> str:
    p, _ = ledger.propose(request_key=key, intent=Intent("seller.example", "0xa1", item, 40), tool_call={})
    ledger.evaluate_and_reserve(p["id"])
    assert ledger.purchase(p["id"])["stage"] == Stage.RESERVED.value
    return p["id"]


class NoDelivery(Seller):
    def __init__(self):
        pass

    def fetch(self, **_):
        raise RuntimeError("not needed")


def test_mock_purchase_records_rail_and_transaction(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"), policy=POLICY)
    pid = _reserved(ledger)
    out = orchestrator.execute(ledger, MockRail(), NoDelivery(), pid)
    assert out["rail"] == "mock"
    assert out["rail_tx_id"] == out["rail_order_id"] and out["rail_tx_id"]
    assert out["approval_id"] is None


def test_qatom_approval_survives_restart_and_settles_with_final_transaction(tmp_path):
    path = str(tmp_path / "l.db")
    ledger = Ledger(path, policy=POLICY)
    pid = _reserved(ledger)
    first = QatomRail(QatomConfig(item_map={"btc_read": 22}), mcp=FakeMcp([(APPROVAL, False)]), store=ledger)
    p = orchestrator.execute(ledger, first, NoDelivery(), pid)
    assert p["stage"] == Stage.DISPATCHED.value
    assert (p["rail"], p["approval_id"], p["rail_order_id"]) == ("qatom", AID, APPROVAL_PREFIX + AID)
    ledger.conn.close()

    # Gateway restarts: new ledger connection, new rail object, empty memory.
    reopened = Ledger(path)
    second = QatomRail(QatomConfig(item_map={"btc_read": 22}), mcp=FakeMcp([(BOUGHT, False)]), store=reopened)
    assert second.pending == {}
    done = orchestrator.reconcile(reopened, second, NoDelivery(), pid)
    assert done["stage"] in (Stage.SETTLED.value, Stage.RECOVERY_UNDELIVERED.value)
    assert done["rail_tx_id"] == TX and done["approval_id"] == AID
    (_, args), = second.mcp.calls
    assert args == {"catalog_item_id": 22, "approval_id": AID}  # the exact same checkout, resumed
    assert reopened.rail_approval(AID) is None  # resolved, cannot be replayed


def test_lost_approval_context_stays_unknown_instead_of_guessing(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"), policy=POLICY)
    rail = QatomRail(QatomConfig(item_map={"btc_read": 22}), mcp=FakeMcp([]), store=ledger)
    res = rail.get_order(APPROVAL_PREFIX + "aar_never_seen")
    assert res.outcome is RailOutcome.UNKNOWN and res.status == "APPROVAL_CONTEXT_LOST"
    assert rail.mcp.calls == []


def test_old_database_gains_new_columns_and_keeps_rows(tmp_path):
    path = str(tmp_path / "old.db")
    ledger = Ledger(path, policy=POLICY)
    pid = _reserved(ledger)
    ledger.conn.close()
    con = sqlite3.connect(path)  # simulate a database from before MND-32
    for col in ("rail", "approval_id", "rail_tx_id"):
        con.execute(f"ALTER TABLE purchases DROP COLUMN {col}")
    con.commit()
    con.close()
    reopened = Ledger(path)
    row = reopened.purchase(pid)
    assert row["stage"] == Stage.RESERVED.value
    assert row["rail"] is None and row["approval_id"] is None and row["rail_tx_id"] is None
