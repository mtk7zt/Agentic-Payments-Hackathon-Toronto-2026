"""Console approval routes are owner-only and remember the first status."""
import pytest
from fastapi.testclient import TestClient

from backend.api.main import app

TOKEN = "o" * 40
OTHER = "x" * 40


@pytest.fixture(autouse=True)
def owner_token(monkeypatch):
    monkeypatch.setenv("MANDATE_OWNER_TOKEN", TOKEN)


def test_console_routes_require_the_owner_token():
    client = TestClient(app)
    missing = client.post("/api/v1/console-users", json={"email": "a@example.com"})
    assert missing.status_code == 401
    refused = client.post("/api/v1/console-users", json={"email": "a@example.com"},
                          headers={"X-Owner-Token": OTHER})
    assert refused.status_code == 403
    assert client.get("/api/v1/console-users").status_code == 401
    assert client.post("/api/v1/console-users/a@example.com/approve").status_code == 401


def test_register_is_pending_until_the_owner_decides():
    client = TestClient(app, headers={"X-Owner-Token": TOKEN})
    created = client.post("/api/v1/console-users", json={"email": "  Guest@Example.com "})
    assert created.status_code == 200
    body = created.json()
    assert body["email"] == "guest@example.com"
    assert body["status"] == "pending"
    again = client.post("/api/v1/console-users", json={"email": "guest@example.com"}).json()
    assert again["status"] == "pending"
    assert again["created_at"] == body["created_at"]

    listed = client.get("/api/v1/console-users").json()["users"]
    assert any(row["email"] == "guest@example.com" and row["status"] == "pending" for row in listed)

    approved = client.post("/api/v1/console-users/guest@example.com/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    rejected = client.post("/api/v1/console-users/guest@example.com/reject")
    assert rejected.json()["status"] == "rejected"
    assert client.post("/api/v1/console-users/missing@example.com/approve").status_code == 404
    assert client.post("/api/v1/console-users", json={"email": "not-an-email"}).status_code == 422
