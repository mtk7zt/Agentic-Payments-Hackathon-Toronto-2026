"""The live Qatom script, against a fake Qatom: approval, waiting, settling once, resuming, limits."""
from backend.core.ledger import Ledger
from backend.services.qatom_rail import QatomConfig, QatomRail
from scripts import qatom_live

from .test_qatom_rail import APPROVAL, BOUGHT, FakeMcp

TX = "f4488a55-24a6-481c-9cfe-ec2ac4424a98"


def rail(ledger, replies):
    return qatom_live._Recording(QatomRail(QatomConfig(item_map={"qatom-26": 26}), mcp=FakeMcp(replies), store=ledger))


def test_waits_for_approval_then_settles_once_without_a_fake_delivery(capsys):
    ledger = Ledger(":memory:", mandate_id="mnd_live_qatom")
    r = rail(ledger, [(APPROVAL, False), (APPROVAL, False), (BOUGHT, False)])
    p = qatom_live.run(ledger, r, catalog_id=26, amount_milli=1, wait_s=60, poll_s=0, sleep=lambda s: None)
    assert p["stage"] == "SETTLED" and p["rail"] == "qatom" and p["rail_tx_id"] == TX
    assert p["delivery"] == "PENDING"  # Mandate did not pretend to verify Qatom's delivery
    assert "approvals/" in capsys.readouterr().out  # the owner was shown the link
    assert ledger.balances()["settled"] == 1 and ledger.verify_chain()["intact"]
    assert "approvals/" not in str(ledger.events())  # the link is never stored as evidence


def test_timeout_keeps_the_hold_and_resume_finishes_it(capsys):
    ledger = Ledger(":memory:", mandate_id="mnd_live_qatom")
    t = [0.0]
    r = rail(ledger, [(APPROVAL, False), (APPROVAL, False)])
    p = qatom_live.run(ledger, r, catalog_id=26, amount_milli=1, wait_s=5, poll_s=5,
                       sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    assert p["stage"] == "DISPATCHED" and ledger.balances()["held"] == 1
    r2 = rail(ledger, [(BOUGHT, False)])
    done = qatom_live.run(ledger, r2, catalog_id=26, amount_milli=1, resume=p["id"], poll_s=0, sleep=lambda s: None)
    assert done["stage"] == "SETTLED" and r2.rail.mcp.calls[0][1]["approval_id"].startswith("aar_")


def test_amount_above_the_live_limit_is_refused_before_anything_happens(capsys):
    assert qatom_live.main(["--catalog-id", "26", "--amount", "0.101"]) == 2
    assert "above the 0.100" in capsys.readouterr().err


def test_item_inputs_reach_qatom_and_a_refusal_records_why(capsys):
    ledger = Ledger(":memory:", mandate_id="mnd_live_qatom")
    r = rail(ledger, [(BOUGHT, False)])
    p = qatom_live.run(ledger, r, catalog_id=26, amount_milli=1, arguments={"package": "react"},
                       poll_s=0, sleep=lambda s: None)
    assert p["stage"] == "SETTLED"
    assert r.rail.mcp.calls[0][1] == {"catalog_item_id": 26, "arguments": {"package": "react"}}

    ledger = Ledger(":memory:", mandate_id="mnd_live_qatom")
    r = rail(ledger, [("Missing required input: package.", True)])
    p = qatom_live.run(ledger, r, catalog_id=26, amount_milli=1, poll_s=0, sleep=lambda s: None)
    assert p["stage"] == "EXPIRED" and ledger.balances()["held"] == 0
    assert "Missing required input: package." in str(ledger.events())


def test_bad_arg_is_refused_before_anything_happens(capsys):
    assert qatom_live.main(["--catalog-id", "26", "--amount", "0.001", "--arg", "package"]) == 2
    assert "NAME=VALUE" in capsys.readouterr().err
