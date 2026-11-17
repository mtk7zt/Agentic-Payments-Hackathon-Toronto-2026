"""Payment rails behind one interface.

GoatFlowRail talks to the GOAT Flow x402 merchant API. MockRail is a
deterministic stand-in for tests and demos. A Qatom rail plugs in here once
its contract was confirmed (MND-01): see services/qatom_rail.py.

The caller (services/orchestrator.py) records a dispatch claim BEFORE calling
`create_order`, and passes the claim key as `dapp_order_id`. GOAT Flow rejects
duplicate dapp_order_ids, so a replayed request cannot open a second order.
A timeout is reported as UNKNOWN and is never retried here.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

import httpx

from ..core.ids import ulid
from ..models.money import milli_to_token_units
from .signing import sign


class RailOutcome(str, Enum):
    SETTLED = "SETTLED"  # payment confirmed by the rail
    PENDING = "PENDING"  # order exists, payment not confirmed yet
    UNPAID = "UNPAID"  # definitively no payment: failed, expired or cancelled
    UNKNOWN = "UNKNOWN"  # no trustworthy answer; treat exposure as live


@dataclass(frozen=True)
class RailResult:
    outcome: RailOutcome
    order_id: str | None = None
    status: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class PaymentRail(Protocol):
    name: str

    def create_order(self, *, dapp_order_id: str, amount_milli: int, item: str | None = None,
                     arguments: dict[str, Any] | None = None) -> RailResult: ...
    def get_order(self, order_id: str) -> RailResult: ...
    def get_proof(self, order_id: str) -> dict[str, Any]: ...


# GOAT Flow order statuses, from the x402 API reference.
_STATUS: dict[str, RailOutcome] = {
    "CHECKOUT_VERIFIED": RailOutcome.PENDING,
    "PAYMENT_CONFIRMED": RailOutcome.SETTLED,
    "INVOICED": RailOutcome.SETTLED,
    "FAILED": RailOutcome.UNPAID,
    "EXPIRED": RailOutcome.UNPAID,
    "CANCELLED": RailOutcome.UNPAID,
}


@dataclass
class GoatFlowConfig:
    api_url: str = "https://flow-api.testnet3.goat.network"
    api_key: str = ""
    api_secret: str = ""
    merchant_id: str = ""
    chain_id: int = 48816
    token_symbol: str = "USDC"
    token_contract: str = "0x29D1Ee93e9ecf6E50F309f498e40a6b42D352Fa1"
    from_address: str = ""
    timeout_s: float = 10.0

    @classmethod
    def from_env(cls) -> GoatFlowConfig:
        e = os.environ.get
        return cls(
            api_url=e("GOAT_FLOW_API_URL", cls.api_url).rstrip("/"), api_key=e("GOAT_FLOW_API_KEY", ""),
            api_secret=e("GOAT_FLOW_API_SECRET", ""), merchant_id=e("GOAT_FLOW_MERCHANT_ID", ""),
            chain_id=int(e("GOAT_FLOW_CHAIN_ID", "48816")), token_symbol=e("GOAT_FLOW_TOKEN_SYMBOL", "USDC"),
            token_contract=e("GOAT_FLOW_TOKEN_CONTRACT", cls.token_contract),
            from_address=e("GOAT_FLOW_FROM_ADDRESS", ""))

    def require(self) -> None:
        missing = [n for n in ("api_key", "api_secret", "from_address") if not getattr(self, n)]
        if missing:
            raise RuntimeError(f"GOAT Flow config missing: {', '.join(missing)}")


class GoatFlowRail:
    """POST /api/v1/orders · GET /api/v1/orders/{id} · GET /api/v1/orders/{id}/proof"""

    name = "goat-flow-x402"

    def __init__(self, config: GoatFlowConfig, client: httpx.Client | None = None):
        config.require()
        self.cfg = config
        self.http = client or httpx.Client(base_url=config.api_url, timeout=config.timeout_s)

    def _headers(self, params: dict[str, Any]) -> dict[str, str]:
        return sign(params, self.cfg.api_key, self.cfg.api_secret)

    @staticmethod
    def _order_id(body: dict[str, Any]) -> str | None:
        # The reference does not name the response fields; accept the common shapes.
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        for key in ("order_id", "orderId", "id"):
            if isinstance(data.get(key), str):
                return data[key]
        return None

    def _result(self, body: dict[str, Any], default: RailOutcome) -> RailResult:
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        status = data.get("status") if isinstance(data.get("status"), str) else None
        return RailResult(_STATUS.get(status or "", default), self._order_id(body), status, body)

    def create_order(self, *, dapp_order_id: str, amount_milli: int, **_: Any) -> RailResult:
        body: dict[str, Any] = {
            "dapp_order_id": dapp_order_id,
            "chain_id": self.cfg.chain_id,
            "token_symbol": self.cfg.token_symbol,
            "token_contract": self.cfg.token_contract,
            "from_address": self.cfg.from_address,
            "amount_wei": str(milli_to_token_units(amount_milli)),  # integer base units, as a string
        }
        if self.cfg.merchant_id:
            body["merchant_id"] = self.cfg.merchant_id
        try:
            r = self.http.post("/api/v1/orders", json=body, headers=self._headers(body))
        except httpx.HTTPError as exc:  # timeout, reset, DNS: we cannot know what the rail did
            return RailResult(RailOutcome.UNKNOWN, raw={"error": type(exc).__name__})
        payload = _json(r)
        if r.status_code in (200, 202, 402):  # 402 carries the order id and payment terms
            return self._result(payload, RailOutcome.PENDING)
        if r.status_code == 409:  # order already exists for this dapp_order_id: look it up, do not re-create
            return RailResult(RailOutcome.UNKNOWN, raw=payload)
        if 400 <= r.status_code < 500:
            return RailResult(RailOutcome.UNPAID, status=_reason(r.status_code, payload), raw=payload)
        return RailResult(RailOutcome.UNKNOWN, status=_reason(r.status_code, payload), raw=payload)

    def get_order(self, order_id: str) -> RailResult:
        try:
            r = self.http.get(f"/api/v1/orders/{order_id}", headers=self._headers({}))
        except httpx.HTTPError as exc:
            return RailResult(RailOutcome.UNKNOWN, order_id, raw={"error": type(exc).__name__})
        if r.status_code != 200:
            body = _json(r)
            return RailResult(RailOutcome.UNKNOWN, order_id, _reason(r.status_code, body), body)
        res = self._result(_json(r), RailOutcome.UNKNOWN)
        return RailResult(res.outcome, order_id, res.status, res.raw)

    def get_proof(self, order_id: str) -> dict[str, Any]:
        r = self.http.get(f"/api/v1/orders/{order_id}/proof", headers=self._headers({}))
        r.raise_for_status()
        return _json(r)


# A provider message is copied into evidence only if it holds nothing that looks like a credential value.
_TOKENISH = re.compile(r"[A-Za-z0-9_\-+/=]{20,}")


def _reason(status_code: int, payload: dict[str, Any]) -> str:
    """`HTTP_<code>` plus the provider's own error message, so evidence says why an order failed.

    Only message-like fields are kept, trimmed, and never anything that looks like a credential.
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    for key in ("message", "msg", "error", "error_message", "detail", "reason", "code"):
        val = data.get(key) if isinstance(data, dict) else None
        text = provider_message(val) if isinstance(val, (str, int)) else ""
        if text:
            return f"HTTP_{status_code}: {text}"
    return f"HTTP_{status_code}"


def provider_message(value: Any, limit: int = 160) -> str:
    """A provider's message made safe to keep in evidence: one line, trimmed, and empty if it holds
    anything that looks like a credential, link token or other long opaque value."""
    text = " ".join(str(value).split())[:limit]
    return "" if not text or _TOKENISH.search(text) else text


def _json(r: httpx.Response) -> dict[str, Any]:
    try:
        body = r.json()
        return body if isinstance(body, dict) else {"body": body}
    except ValueError:
        return {"text": r.text[:500]}


class MockRail:
    """Sandbox rail for development and tests. Never configured in production mode.

    `behaviour` maps a substring of the item name or of the dapp_order_id to a scripted outcome;
    anything unmatched settles. Order ids are unique, like a real provider's.
    """

    name = "mock"

    def __init__(self, behaviour: dict[str, RailOutcome] | None = None):
        self.behaviour = behaviour or {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.create_calls = 0

    def create_order(self, *, dapp_order_id: str, amount_milli: int, item: str | None = None, **_: Any) -> RailResult:
        self.create_calls += 1
        if dapp_order_id in self.orders:  # mirrors GOAT Flow: duplicates are rejected, never re-opened
            return RailResult(RailOutcome.UNKNOWN, raw={"error": "order already exists"})
        outcome = next((o for k, o in self.behaviour.items() if k in dapp_order_id or (item and k in item)),
                       RailOutcome.SETTLED)
        order_id = f"mock_{ulid()}"
        self.orders[dapp_order_id] = {"order_id": order_id, "amount_wei": str(milli_to_token_units(amount_milli))}
        if outcome is RailOutcome.UNKNOWN:  # the order may exist on the rail, but we never heard back
            return RailResult(RailOutcome.UNKNOWN, raw={"error": "ReadTimeout"})
        return RailResult(outcome, order_id, "PAYMENT_CONFIRMED" if outcome is RailOutcome.SETTLED else outcome.value)

    def get_order(self, order_id: str) -> RailResult:
        return RailResult(RailOutcome.SETTLED, order_id, "PAYMENT_CONFIRMED")

    def find_by_dapp_order_id(self, dapp_order_id: str) -> str | None:
        o = self.orders.get(dapp_order_id)
        return o["order_id"] if o else None

    def get_proof(self, order_id: str) -> dict[str, Any]:
        return {"order_id": order_id, "signature": "mock-proof", "mock": True}


def rail_from_env(choice: str | None = None, store: Any | None = None) -> PaymentRail:
    """Build the named rail (default: MANDATE_RAIL). `store` (the ledger) lets rails persist pending approvals.

    An unknown name is an error, never a silent fallback to the sandbox rail.
    """
    choice = (choice or os.environ.get("MANDATE_RAIL", "")).lower()
    if choice == "goat":
        return GoatFlowRail(GoatFlowConfig.from_env())
    if choice == "qatom":
        from .qatom_rail import QatomConfig, QatomRail
        return QatomRail(QatomConfig.from_env(), store=store)
    if choice == "mock":
        return MockRail()
    raise ValueError(f"unknown payment rail {choice!r}; use goat, qatom or mock (sandbox only)")
