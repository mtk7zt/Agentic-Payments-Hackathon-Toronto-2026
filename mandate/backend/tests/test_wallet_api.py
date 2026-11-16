from fastapi.testclient import TestClient

from backend.api import main
from backend.core.ledger import Ledger
from backend.core.policy import Policy


def test_wallet_owner_api_validates_money_and_reports_provenance(monkeypatch):
    monkeypatch.setattr(main, 'ledger', Ledger(policy=Policy(task_budget_cap=100)))
    monkeypatch.setenv('MANDATE_OWNER_TOKEN', 'owner-secret')
    client = TestClient(main.app)
    headers = {'X-Owner-Token': 'owner-secret'}
    body = {'balance_milli': 100, 'reference': 'owner: explorer snapshot'}
    assert client.post("/api/v1/wallet/configure", json=body).status_code == 401
    assert client.get('/api/v1/wallet', headers=headers).json()['configured'] is False
    for bad in [True, 100.0, '100', -1, 2**53]:
        assert client.post('/api/v1/wallet/configure', headers=headers,
                           json={**body, 'balance_milli': bad}).status_code == 422
    response = client.post('/api/v1/wallet/configure', headers=headers, json=body)
    assert response.status_code == 200
    assert response.json()['provenance'] == 'owner_recorded'
    assert client.post('/api/v1/wallet/observations', headers=headers,
                       json={**body, 'balance_milli': 90}).json()['mismatch'] is True
    assert client.post('/api/v1/wallet/reconcile', headers=headers, json=body).json()['mismatch'] is False
