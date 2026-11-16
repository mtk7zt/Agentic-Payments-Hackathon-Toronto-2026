"""Controlled seller contract and mock checkout integration; no live payment."""

import json
import secrets
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.api import main as gateway
from backend.core.ledger import Ledger
from backend.core.policy import Intent, Policy
from backend.models.money import parse_milli
from backend.seller_api.main import create_app
from backend.services import orchestrator
from backend.services.payment_rail import MockRail, RailOutcome
from backend.services.seller import HttpSeller, MockSeller, seller_from_env

ITEMS = [("shipping-estimate", 40), ("supplier-summary", 60)]
SELLER = ("seller-a.example", "0x00000000000000000000000000000000000000a1")


@pytest.fixture
def token():
    return secrets.token_urlsafe(32)


@pytest.fixture
def seller_api(token):
    with TestClient(create_app(token=token)) as client:
        yield client


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def body():
    return {"request_id": "request-1", "rail_order_id": "mock_order_1"}


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic wrong", "Bearer "])
@pytest.mark.parametrize("method,path", [
    ("GET", "/api/v1/catalog"), ("POST", "/api/v1/items/shipping-estimate/fulfill"),
])
def test_unauthorized_seller_routes_are_hidden(seller_api, token, authorization, method, path):
    request_headers = {"Authorization": authorization} if authorization is not None else {}
    response = seller_api.request(method, path, json=body(), headers=request_headers)
    assert response.status_code == 404
    assert token not in response.text


def test_authentication_precedes_json_validation(seller_api):
    response = seller_api.post(
        "/api/v1/items/shipping-estimate/fulfill", content="{", headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 404


def test_unconfigured_seller_has_no_public_access(monkeypatch):
    monkeypatch.delenv("MANDATE_SELLER_TOKEN", raising=False)
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/catalog").status_code == 404
        assert client.get("/api/v1/catalog", headers=headers("anything")).status_code == 404


def test_catalog_contains_two_exact_fixed_price_synthetic_items(seller_api, token):
    response = seller_api.get("/api/v1/catalog", headers=headers(token))
    assert response.status_code == 200
    items = response.json()["items"]
    assert [(item["item"], item["amount_milli"]) for item in items] == ITEMS
    for item in items:
        assert parse_milli(item["amount"]) == item["amount_milli"]
        assert type(item["amount_milli"]) is int
        assert item["unit"] == "USD-TDN" and item["synthetic"] is True
        assert item["input_schema"]["additionalProperties"] is False
    assert token not in response.text


@pytest.mark.parametrize("item,amount", ITEMS)
def test_seller_delivery_has_exact_amount_correlation_and_timestamp(seller_api, token, item, amount):
    response = seller_api.post(f"/api/v1/items/{item}/fulfill", json=body(), headers=headers(token))
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == "mandate.seller-delivery.v1"
    assert payload["request_id"] == body()["request_id"]
    assert payload["rail_order_id"] == body()["rail_order_id"]
    assert payload["item"] == item and payload["synthetic"] is True
    assert payload["amount_milli"] == amount == parse_milli(payload["amount"])
    assert isinstance(payload["data"], dict)
    timestamp = datetime.fromisoformat(payload["created_at"])
    assert timestamp.tzinfo is not None
    assert abs((datetime.now(UTC) - timestamp).total_seconds()) < 10
    assert token not in response.text


@pytest.mark.parametrize("changes", [
    {"extra": "not permitted"}, {"amount": "0.001"}, {"request_id": 123},
    {"request_id": ""}, {"rail_order_id": ""}, {"rail_order_id": None},
])
def test_seller_input_is_strict(seller_api, token, changes):
    response = seller_api.post(
        "/api/v1/items/shipping-estimate/fulfill", json=body() | changes, headers=headers(token),
    )
    assert response.status_code == 422


def test_unknown_item_is_hidden(seller_api, token):
    assert seller_api.post(
        "/api/v1/items/unknown/fulfill", json=body(), headers=headers(token),
    ).status_code == 404


def bridge(api, seen):
    def handler(request):
        seen.append(request)
        response = api.request(
            request.method, request.url.raw_path.decode(), content=request.content,
            headers={"Authorization": request.headers.get("Authorization", ""), "Content-Type": "application/json"},
        )
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)
    return handler


@pytest.mark.parametrize("item,amount", ITEMS)
def test_mock_checkout_delivers_real_seller_handler_after_settlement(seller_api, token, item, amount):
    seen = []
    rail = MockRail()
    ledger = Ledger(":memory:", policy=Policy(whitelisted_sellers=dict([SELLER])))
    with httpx.Client(base_url="http://seller.test", transport=httpx.MockTransport(bridge(seller_api, seen))) as client:
        seller = HttpSeller("http://seller.test", token, client)
        catalog = seller.catalog()
        entry = next(entry for entry in catalog if entry["item"] == item)
        assert entry["amount_milli"] == amount
        try:
            purchase = orchestrator.submit(
                ledger, rail, seller, request_key="controlled-checkout",
                intent=Intent(*SELLER, item, amount), tool_call={"tool": "buy_supplier_data"},
            )
            assert purchase["stage"] == "VERIFIED" and rail.create_calls == 1
            assert ledger.balances() == {"budget": 5000, "available": 5000 - amount, "held": 0, "settled": amount}
            events = [event["type"] for event in ledger.events()]
            assert events.index("PAYMENT_SETTLED") < events.index("DELIVERY_VERIFIED")
            assert [request.method for request in seen] == ["GET", "POST"]
            assert json.loads(seen[-1].content) == {
                "request_id": purchase["id"], "rail_order_id": purchase["rail_order_id"],
            }
            replay = orchestrator.submit(
                ledger, rail, seller, request_key="controlled-checkout",
                intent=Intent(*SELLER, item, amount), tool_call={"tool": "buy_supplier_data"},
            )
            assert replay["id"] == purchase["id"] and rail.create_calls == 1 and len(seen) == 2
            assert ledger.verify_chain()["intact"] and token not in json.dumps(ledger.events())
        finally:
            ledger.conn.close()


@pytest.mark.parametrize("outcome", [RailOutcome.PENDING, RailOutcome.UNKNOWN, RailOutcome.UNPAID])
def test_unsettled_purchase_never_fetches_seller(seller_api, token, outcome):
    seen = []
    ledger = Ledger(":memory:", policy=Policy(whitelisted_sellers=dict([SELLER])))
    with httpx.Client(base_url="http://seller.test", transport=httpx.MockTransport(bridge(seller_api, seen))) as client:
        try:
            orchestrator.submit(
                ledger, MockRail({"shipping-estimate": outcome}), HttpSeller("http://seller.test", token, client),
                request_key="unsettled-checkout", intent=Intent(*SELLER, "shipping-estimate", 40), tool_call={},
            )
            assert seen == [] and ledger.balances()["settled"] == 0
        finally:
            ledger.conn.close()


def test_seller_auth_failure_keeps_payment_settled_and_recovery_never_recharges(seller_api, token):
    seen = []
    rail = MockRail()
    ledger = Ledger(":memory:", policy=Policy(whitelisted_sellers=dict([SELLER])))
    with httpx.Client(base_url="http://seller.test", transport=httpx.MockTransport(bridge(seller_api, seen))) as client:
        try:
            purchase = orchestrator.submit(
                ledger, rail, HttpSeller("http://seller.test", "wrong", client), request_key="seller-failure",
                intent=Intent(*SELLER, "shipping-estimate", 40), tool_call={},
            )
            assert purchase["stage"] == "RECOVERY_UNDELIVERED" and ledger.balances()["settled"] == 40
            recovered = orchestrator.reconcile(ledger, rail, HttpSeller("http://seller.test", token, client), purchase["id"])
            assert recovered["stage"] == "VERIFIED" and rail.create_calls == 1
            assert ledger.balances()["settled"] == 40 and ledger.verify_chain()["intact"]
        finally:
            ledger.conn.close()


def test_http_seller_configuration_is_explicit_and_secret_free(monkeypatch):
    monkeypatch.delenv("MANDATE_SELLER_MODE", raising=False)
    with pytest.raises(ValueError, match="mock or http"):
        seller_from_env()  # no silent fallback to the sandbox seller
    assert isinstance(seller_from_env("mock"), MockSeller)
    monkeypatch.setenv("MANDATE_SELLER_MODE", "http")
    monkeypatch.delenv("MANDATE_SELLER_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="MANDATE_SELLER_TOKEN"):
        seller_from_env()


def test_gateway_purchase_endpoint_checks_out_against_controlled_seller(seller_api, token, monkeypatch):
    seen = []
    ledger = Ledger(":memory:", policy=Policy(whitelisted_sellers=dict([SELLER])))
    rail = MockRail()
    monkeypatch.setenv("MANDATE_OWNER_TOKEN", token)
    with httpx.Client(base_url="http://seller.test", transport=httpx.MockTransport(bridge(seller_api, seen))) as client:
        monkeypatch.setattr(gateway, "ledger", ledger)
        monkeypatch.setattr(gateway, "rail", rail)
        monkeypatch.setattr(gateway, "seller", HttpSeller("http://seller.test", token, client))
        try:
            with TestClient(gateway.app) as api:
                session = api.post("/api/v1/session-keys", headers={"X-Owner-Token": token}).json()["token"]
                response = api.post("/api/v1/purchases", headers={"X-Session-Key": session}, json={
                    "request_key": "http-gateway-checkout", "seller_domain": SELLER[0], "seller_address": SELLER[1],
                    "item": "shipping-estimate", "amount": "0.040", "tool_call": {"tool": "buy_supplier_data"},
                })
                assert response.status_code == 201
                purchase = response.json()
                assert purchase["stage"] == "VERIFIED" and purchase["amount"] == "0.040"
                assert purchase["amount_milli"] == 40 and rail.create_calls == 1
                assert len(seen) == 1 and ledger.balances()["settled"] == 40
                assert ledger.verify_chain()["intact"]
        finally:
            ledger.conn.close()


def test_redirect_never_forwards_the_seller_token(token):
    seen = []

    def redirect(request):
        seen.append(request)
        return httpx.Response(307, headers={"Location": "https://attacker.example/fulfill"})

    with (
        httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=True) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        HttpSeller("http://seller.test", token, client).fetch(item="shipping-estimate", **body())
    assert len(seen) == 1 and seen[0].url.host == "seller.test"


@pytest.mark.parametrize("amount", [True, 40.0, 2**53])
def test_http_catalog_refuses_non_integer_or_unsafe_money(token, amount):
    with (
        httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"items": [{"amount": "0.040", "amount_milli": amount}]}),
        )) as client,
        pytest.raises(ValueError),
    ):
        HttpSeller("http://seller.test", token, client).catalog()


@pytest.mark.parametrize("field,value", [("request_id", "other"), ("rail_order_id", "other"), ("item", "other")])
def test_http_seller_refuses_mismatched_response(field, value, token):
    payload = body() | {"item": "shipping-estimate", "data": {}, "created_at": datetime.now(UTC).isoformat()}
    payload[field] = value
    with (
        httpx.Client(base_url="http://seller.test", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload),
        )) as client,
        pytest.raises(ValueError, match="correlation"),
    ):
        HttpSeller("http://seller.test", token, client).fetch(item="shipping-estimate", **body())
