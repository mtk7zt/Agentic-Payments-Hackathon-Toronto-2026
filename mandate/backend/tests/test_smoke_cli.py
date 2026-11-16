import json
import subprocess
import sys
from pathlib import Path

import pytest

from backend.services.payment_rail import MockRail, RailOutcome
from scripts import smoke


def receipt(output):
    return json.loads(output.split("\nreceipt ", 1)[1])


def test_default_mock_prints_stored_purchase_and_balances(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["smoke.py"])
    assert smoke.main() == 0
    saved = receipt(capsys.readouterr().out)
    assert saved["rail"] == "mock" and len(saved["purchases"]) == 1
    purchase = saved["purchases"][0]
    assert purchase["stage"] == "VERIFIED" and purchase["amount_milli"] == 40
    assert purchase["rail_order_id"].startswith("mock_")
    assert saved["balances_milli"] == {"budget": 5000, "held": 0, "settled": 40, "available": 4960}
    assert saved["chain"]["intact"]


def test_unknown_outcome_is_nonzero_and_retains_hold(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["smoke.py"])
    monkeypatch.setattr(smoke, "MockRail", lambda *args: MockRail({"pur_0001": RailOutcome.UNKNOWN}))
    assert smoke.main() == 1
    saved = receipt(capsys.readouterr().out)
    assert saved["purchases"][0]["stage"] == "RECOVERY_UNKNOWN"
    assert saved["balances_milli"]["held"] == 40 and saved["balances_milli"]["settled"] == 0


@pytest.mark.parametrize("outcome", [RailOutcome.PENDING, RailOutcome.UNPAID])
def test_pending_or_unpaid_cannot_pass_payment_smoke(monkeypatch, capsys, outcome):
    monkeypatch.setattr(sys, "argv", ["smoke.py"])
    monkeypatch.setattr(smoke, "MockRail", lambda *args: MockRail({"pur_0001": outcome}))
    assert smoke.main() == 1
    saved = receipt(capsys.readouterr().out)
    assert saved["balances_milli"]["settled"] == 0


def test_broken_evidence_is_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["smoke.py"])
    monkeypatch.setattr(smoke.Ledger, "verify_chain", lambda self: {"intact": False, "first_bad_seq": 1})
    assert smoke.main() == 1
    assert receipt(capsys.readouterr().out)["chain"]["intact"] is False


def test_scenarios_exit_nonzero_for_expected_unknown_from_real_process():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, "scripts/smoke.py", "--scenarios"], cwd=root,
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 1
    assert "RECOVERY_UNKNOWN" in result.stdout and "retry after timeout refused" in result.stdout


def test_live_missing_configuration_fails_before_network(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["smoke.py", "--live"])
    for name in ("GOAT_FLOW_API_KEY", "GOAT_FLOW_API_SECRET", "GOAT_FLOW_FROM_ADDRESS"):
        monkeypatch.delenv(name, raising=False)
    assert smoke.main() == 2
    assert "configuration" in capsys.readouterr().err


def test_live_invocations_use_unique_keys_and_never_retry_create(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["smoke.py", "--live"])
    rail = MockRail()
    config = smoke.GoatFlowConfig(api_key="synthetic", api_secret="synthetic", from_address="synthetic")
    monkeypatch.setattr(smoke.GoatFlowConfig, "from_env", lambda: config)
    monkeypatch.setattr(smoke, "GoatFlowRail", lambda cfg: rail)
    assert smoke.main() == 0
    first = receipt(capsys.readouterr().out)["purchases"][0]["request_key"]
    assert smoke.main() == 0
    second = receipt(capsys.readouterr().out)["purchases"][0]["request_key"]
    assert first != second and rail.create_calls == 2


def test_live_refuses_wrong_origin_without_constructing_transport(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["smoke.py", "--live"])
    config = smoke.GoatFlowConfig(api_key="synthetic", api_secret="synthetic", from_address="synthetic", api_url="https://example.invalid")
    monkeypatch.setattr(smoke.GoatFlowConfig, "from_env", lambda: config)
    monkeypatch.setattr(smoke, "GoatFlowRail", lambda cfg: pytest.fail("transport must not be created"))
    assert smoke.main() == 2
    assert "configuration" in capsys.readouterr().err
