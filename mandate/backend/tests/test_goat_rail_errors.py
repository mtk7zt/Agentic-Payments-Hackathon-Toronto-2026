"""GOAT Flow refusals keep the provider's reason in evidence, never a credential."""
import httpx

from backend.services.payment_rail import GoatFlowConfig, GoatFlowRail, RailOutcome

CFG = GoatFlowConfig(api_key="k", api_secret="s", from_address="0xabc")


def rail(status, body):
    return GoatFlowRail(CFG, client=httpx.Client(base_url=CFG.api_url,
                                                 transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body))))


def test_refusal_keeps_the_provider_message():
    res = rail(400, {"code": 40001, "message": "merchant_id is required"}).create_order(dapp_order_id="d", amount_milli=40)
    assert res.outcome is RailOutcome.UNPAID and res.status == "HTTP_400: merchant_id is required"


def test_nested_error_message_is_found():
    res = rail(401, {"data": {"error": "invalid signature"}}).create_order(dapp_order_id="d", amount_milli=40)
    assert res.status == "HTTP_401: invalid signature"


def test_messages_that_mention_credentials_are_not_copied():
    res = rail(401, {"message": "api_key sk_live_9f8e7d6c5b4a39281706 is not valid"}).create_order(dapp_order_id="d", amount_milli=40)
    assert res.status == "HTTP_401"


def test_server_errors_stay_unknown_with_reason():
    res = rail(503, {"message": "maintenance"}).create_order(dapp_order_id="d", amount_milli=40)
    assert res.outcome is RailOutcome.UNKNOWN and res.status == "HTTP_503: maintenance"
