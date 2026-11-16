"""MND-30: overlapping owner reads must never crash or see a torn snapshot."""
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from backend.api import main
from backend.core.ledger import Ledger
from backend.core.policy import Policy


def test_state_and_wallet_reads_survive_heavy_overlap(monkeypatch, tmp_path):
    ledger = Ledger(path=str(tmp_path / "ledger.db"), policy=Policy(task_budget_cap=5000))
    monkeypatch.setattr(main, "ledger", ledger)
    monkeypatch.setenv("MANDATE_OWNER_TOKEN", "owner-secret")
    client = TestClient(main.app)
    headers = {"X-Owner-Token": "owner-secret"}
    client.post("/api/v1/wallet/configure", headers=headers, json={"balance_milli": 5000, "reference": "owner: seed"})

    def hit(i: int) -> int:
        path = ("/api/v1/state", "/api/v1/wallet", "/api/v1/evidence/verify")[i % 3]
        return client.get(path, headers=headers).status_code

    with ThreadPoolExecutor(max_workers=16) as pool:
        codes = list(pool.map(hit, range(300)))
    assert codes.count(200) == len(codes), {c: codes.count(c) for c in set(codes)}


def test_reads_interleaved_with_writes_stay_consistent(monkeypatch, tmp_path):
    ledger = Ledger(path=str(tmp_path / "ledger.db"), policy=Policy(task_budget_cap=5000))
    monkeypatch.setattr(main, "ledger", ledger)
    monkeypatch.setenv("MANDATE_OWNER_TOKEN", "owner-secret")
    client = TestClient(main.app)
    headers = {"X-Owner-Token": "owner-secret"}

    def read(_: int) -> dict:
        r = client.get("/api/v1/state", headers=headers)
        assert r.status_code == 200, r.text
        return r.json()

    def write(i: int) -> int:
        return client.post("/api/v1/wallet/observations", headers=headers,
                           json={"balance_milli": 5000 - i, "reference": f"owner: obs {i}"}).status_code

    client.post("/api/v1/wallet/configure", headers=headers, json={"balance_milli": 5000, "reference": "owner: seed"})
    with ThreadPoolExecutor(max_workers=12) as pool:
        reads = [pool.submit(read, i) for i in range(120)]
        writes = [pool.submit(write, i) for i in range(40)]
        states = [f.result() for f in reads]
        assert all(f.result() == 200 for f in writes)
    for s in states:
        b = s["balances"]
        assert b["available"] == b["budget"] - b["held"] - b["settled"]
        assert s["chain"]["intact"] is True
