"""Qatom adapter: the two-call approval contract observed in MND-01."""
import httpx

from backend.services.payment_rail import RailOutcome
from backend.services.qatom_rail import (
    APPROVAL_PREFIX,
    McpClient,
    QatomConfig,
    QatomRail,
)

APPROVAL = ("Not done yet: the user has to approve buying \"BTC Options Market Read\" for 0.000. Nothing has been charged or moved.\n"
            "https://pay.m.todaq.net/v4/agent/approvals/aar_37b275ee-ef60-47f0-b9c2-bcdd2051f858/review\n"
            "Approval id: aar_37b275ee-ef60-47f0-b9c2-bcdd2051f858.")
BOUGHT = ("Bought \"BTC Options Market Read\" for 0.000 USD-TDN from your agent wallet. "
          "Transaction: f4488a55-24a6-481c-9cfe-ec2ac4424a98\n{}")


class FakeMcp:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self.replies.pop(0)


def rail(replies, item_map=None):
    return QatomRail(QatomConfig(item_map=item_map or {"btc_read": 22}), mcp=FakeMcp(replies))


def test_unmapped_item_never_calls_qatom():
    r = rail([])
    res = r.create_order(dapp_order_id="k1", amount_milli=1000, item="espresso")
    assert res.outcome is RailOutcome.UNPAID and res.status == "ITEM_NOT_ON_QATOM"
    assert r.mcp.calls == []


def test_approval_then_settle_with_same_item_and_arguments():
    r = rail([(APPROVAL, False), (BOUGHT, False)])
    first = r.create_order(dapp_order_id="k1", amount_milli=0, item="btc_read", arguments={"asset": "BTC"})
    assert first.outcome is RailOutcome.PENDING
    assert first.order_id == APPROVAL_PREFIX + "aar_37b275ee-ef60-47f0-b9c2-bcdd2051f858"
    assert first.raw["approval_url"].endswith("/review")
    done = r.get_order(first.order_id)
    assert done.outcome is RailOutcome.SETTLED and done.order_id == "f4488a55-24a6-481c-9cfe-ec2ac4424a98"
    (_, a1), (_, a2) = r.mcp.calls
    assert a2 == {**a1, "approval_id": "aar_37b275ee-ef60-47f0-b9c2-bcdd2051f858"}


def test_charge_above_reservation_is_not_treated_as_settled():
    r = rail([(BOUGHT.replace("0.000 USD", "0.250 USD"), False)])
    res = r.create_order(dapp_order_id="k1", amount_milli=100, item="btc_read")
    assert res.outcome is RailOutcome.UNKNOWN and res.status == "PRICE_ABOVE_RESERVED"


def test_insufficient_funds_is_unpaid():
    r = rail([("The agent wallet cannot cover the price.", True)])
    assert r.create_order(dapp_order_id="k", amount_milli=10, item="btc_read").outcome is RailOutcome.UNPAID


def test_network_error_is_unknown_and_not_retried():
    class Boom:
        def __init__(self):
            self.calls = []

        def call_tool(self, *a):
            self.calls.append(a)
            raise httpx.ReadTimeout("t")
    r = QatomRail(QatomConfig(item_map={"btc_read": 22}), mcp=Boom())
    assert r.create_order(dapp_order_id="k", amount_milli=10, item="btc_read").outcome is RailOutcome.UNKNOWN
    assert len(r.mcp.calls) == 1


def test_mcp_client_initializes_once_and_reads_sse():
    seen = []

    def handler(req):
        body = req.read().decode()
        seen.append((body, req.headers.get("mcp-session-id"), req.headers["authorization"]))
        if '"initialize"' in body:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}}, headers={"mcp-session-id": "s1"})
        data = '{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"hello"}]}}'
        return httpx.Response(200, text=f"event: message\ndata: {data}\n\n", headers={"content-type": "text/event-stream"})
    c = McpClient("https://x/mcp", "tok", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert c.call_tool("agent_wallet_info", {}) == ("hello", False)
    assert seen[1][1] == "s1" and seen[0][2] == "Bearer tok"


def test_refusal_keeps_qatom_reason_without_credential_like_values():
    r = rail([("Missing required input: package. Approve at https://x/aar_37b275ee-ef60-47f0-b9c2-bcdd2051f858", True)])
    res = r.create_order(dapp_order_id="k", amount_milli=1, item="btc_read")
    assert res.outcome is RailOutcome.UNPAID and res.status == "REFUSED"  # message held an opaque id: dropped
    r = rail([("Missing required input: package.", True)])
    res = r.create_order(dapp_order_id="k", amount_milli=1, item="btc_read")
    assert res.status == "REFUSED: Missing required input: package."
