import json
import secrets
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.api import main as gateway
from backend.api.main import app
from backend.core.ledger import Ledger
from backend.core.policy import Policy
from backend.core.sessions import SessionState
from backend.mcp_server.server import Config, create_app
from backend.services.payment_rail import MockRail
from backend.services.seller import MockSeller


@pytest.fixture
def bridge(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Ledger, MockRail, str]]:
    ledger = Ledger(":memory:", policy=Policy(whitelisted_sellers={"seller.example": "0xa1"}))
    rail = MockRail()
    _, key = ledger.issue_session_key()
    monkeypatch.setattr(gateway, "ledger", ledger)
    monkeypatch.setattr(gateway, "rail", rail)
    monkeypatch.setattr(gateway, "seller", MockSeller())
    with TestClient(create_app(Config(), gateway_transport=httpx.ASGITransport(app=app))) as client:
        client.headers.update({"Authorization": f"Bearer {key}", "Accept": "application/json, text/event-stream"})
        yield client, ledger, rail, key
    ledger.conn.close()


def rpc(client: TestClient, method: str, params: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    assert response.status_code == 200, response.text
    return response.json()["result"]


def propose(client: TestClient, **changes: Any) -> dict[str, Any]:
    return rpc(client, "tools/call", {"name": "propose_purchase", "arguments": {
        "seller_domain": "seller.example", "seller_address": "0xa1", "item": "item", "amount": "0.040",
        "request_key": "mcp-test-request", **changes}})


def test_tool_schemas(bridge: tuple[TestClient, Ledger, MockRail, str]) -> None:
    tools = rpc(bridge[0], "tools/list", {})["tools"]
    assert {tool["name"] for tool in tools} == {"catalog", "propose_purchase", "purchase_status"}
    assert all(tool["inputSchema"]["additionalProperties"] is False for tool in tools)
    schema = next(tool["inputSchema"] for tool in tools if tool["name"] == "propose_purchase")
    assert schema["properties"]["amount"]["type"] == "string"
    assert "request_key" in schema["required"]


def test_success_replay_changed_amount_and_status(bridge: tuple[TestClient, Ledger, MockRail, str]) -> None:
    client, _, rail, _ = bridge
    first = propose(client)["structuredContent"]
    assert first["stage"] == "VERIFIED" and first["amount"] == "0.040"
    assert propose(client)["structuredContent"]["id"] == first["id"] and rail.create_calls == 1
    changed = propose(client, amount="0.050")
    assert changed["isError"] and "request_key reused" in changed["content"][0]["text"]
    assert rpc(client, "tools/call", {"name": "purchase_status", "arguments": {"purchase_id": first["id"]}})["structuredContent"]["id"] == first["id"]
    assert not rpc(client, "tools/call", {"name": "catalog", "arguments": {}}).get("isError")


def test_injected_seller_never_calls_rail(bridge: tuple[TestClient, Ledger, MockRail, str]) -> None:
    client, _, rail, _ = bridge
    result = propose(client, seller_domain="attacker.eth")["structuredContent"]
    assert result["stage"] == "BLOCKED" and result["clause"] == "VENDOR_NOT_WHITELISTED"
    assert rail.create_calls == 0


def test_missing_bearer_revoked_and_origin(bridge: tuple[TestClient, Ledger, MockRail, str]) -> None:
    client, ledger, _, key = bridge
    response = client.post("/mcp", headers={"Authorization": ""}, json={})
    assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"
    assert client.post("/mcp", headers={"Origin": "https://unknown.invalid"}, json={}).status_code == 403
    key_id = ledger.sessions.resolve(key)[0]
    ledger.change_session_state(key_id, SessionState.REVOKED)
    result = propose(client)
    assert result["isError"] and "session key missing, frozen or revoked" in result["content"][0]["text"]
    assert key not in json.dumps(result)


@pytest.mark.parametrize("changes", [{"amount": 0.04}, {"arguments": {str(i): 0 for i in range(21)}},
                                     {"arguments": {"x": "x" * 501}}, {"arguments": {"a": {"b": {"c": {}}}}},
                                     {"arguments": {"a": {str(i): 0 for i in range(20)}}}])
def test_invalid_inputs_never_dispatch(bridge: tuple[TestClient, Ledger, MockRail, str], changes: dict[str, Any]) -> None:
    result = propose(bridge[0], **changes)
    assert result["isError"] and bridge[2].create_calls == 0


def test_pending_guidance_and_valid_argument_boundaries(bridge: tuple[TestClient, Ledger, MockRail, str]) -> None:
    result = propose(bridge[0], amount="0.200", arguments={"a": {"b": {"c": "x" * 500}}})
    assert result["structuredContent"]["stage"] == "PENDING_APPROVAL"
    assert "do not retry" in result["structuredContent"]["guidance"]


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
def test_timeout_is_one_call_and_keeps_original_request_key(failure: type[httpx.HTTPError]) -> None:
    calls = []
    def timeout(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise failure("synthetic", request=request)
    with TestClient(create_app(Config(), gateway_transport=httpx.MockTransport(timeout))) as client:
        client.headers.update({"Authorization": f"Bearer {secrets.token_urlsafe(32)}", "Accept": "application/json, text/event-stream"})
        result = propose(client)["structuredContent"]
    assert result["outcome"] == "unknown" and result["request_key"] == "mcp-test-request"
    assert "EXACTLY the same request_key" in result["instruction"] and len(calls) == 1


def test_origin_auth_gates_and_configured_origin() -> None:
    calls: list[httpx.Request] = []
    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"items": []})
    config = Config(allowed_origins=("https://client.example",))
    with TestClient(create_app(config, gateway_transport=httpx.MockTransport(respond))) as client:
        assert client.post("/mcp", json={}).status_code == 401
        assert client.get("/mcp", headers={"Origin": "null"}).status_code == 403
        assert calls == []
        client.headers.update({"Authorization": "Bearer synthetic-agent", "Origin": "https://client.example",
                               "Accept": "application/json, text/event-stream"})
        initialized = rpc(client, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                               "clientInfo": {"name": "test", "version": "1"}})
        assert initialized["serverInfo"]["name"] == "Mandate"
        rpc(client, "tools/call", {"name": "catalog", "arguments": {}})
        assert len(calls) == 1 and calls[0].headers["X-Session-Key"] == "synthetic-agent"
        assert "authorization" not in calls[0].headers
        client.headers["Authorization"] = "Bearer another-synthetic-agent"
        rpc(client, "tools/call", {"name": "catalog", "arguments": {}})
        assert calls[1].headers["X-Session-Key"] == "another-synthetic-agent"


@pytest.mark.parametrize("arguments", [{str(i): "ok" for i in range(20)}, {"a": [["ok"]]}])
def test_valid_argument_limits(bridge: tuple[TestClient, Ledger, MockRail, str], arguments: dict[str, Any]) -> None:
    assert propose(bridge[0], arguments=arguments)["structuredContent"]["stage"] == "VERIFIED"


def test_unknown_parameter_is_rejected(bridge: tuple[TestClient, Ledger, MockRail, str]) -> None:
    assert propose(bridge[0], extra="not allowed")["isError"]
    assert bridge[2].create_calls == 0


@pytest.mark.parametrize("status", [401, 403, 422])
def test_gateway_errors_are_tool_errors(status: int) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "synthetic gateway validation detail"})
    with TestClient(create_app(Config(), gateway_transport=httpx.MockTransport(respond))) as client:
        client.headers.update({"Authorization": "Bearer synthetic-agent", "Accept": "application/json, text/event-stream"})
        result = propose(client)
    assert result["isError"]
    expected = "session key missing, frozen or revoked" if status in (401, 403) else "synthetic gateway validation detail"
    assert json.loads(result["content"][0]["text"]) == expected
