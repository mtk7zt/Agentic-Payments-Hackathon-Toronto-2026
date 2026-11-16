from backend.core.ledger import Ledger


def test_last_reconciled_wallet_is_not_overwritten_by_mismatch():
    ledger = Ledger(":memory:")
    try:
        ledger.configure_wallet(6000, "synthetic opening")
        first = ledger.wallet_status()
        ledger.record_wallet_observation(5999, "synthetic mismatch")
        mismatch = ledger.wallet_status()
        assert mismatch["observed_milli"] == 5999 and mismatch["mismatch"]
        assert mismatch["last_reconciled_milli"] == 6000
        assert mismatch["last_reconciled_at"] == first["last_reconciled_at"]
        ledger.reconcile_wallet(5999, "owner acknowledged synthetic movement")
        assert ledger.wallet_status()["last_reconciled_milli"] == 5999
    finally:
        ledger.conn.close()
