"""Seller delivery check. Runs after settlement and is recorded separately."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from ..models.money import parse_milli, validate_milli


@dataclass(frozen=True)
class Delivery:
    ok: bool
    detail: str


class Seller(Protocol):
    def fetch(self, *, item: str, rail_order_id: str, request_id: str) -> dict[str, Any]: ...


def verify_delivery(payload: dict[str, Any] | None, *, item: str, request_id: str) -> Delivery:
    """Schema, request match and size only. This does not judge whether the data is true."""
    if not isinstance(payload, dict):
        return Delivery(False, "Seller returned no usable payload")
    if payload.get("request_id") != request_id:
        return Delivery(False, "Payload does not match this request")
    if payload.get("item") != item or "data" not in payload:
        return Delivery(False, "Payload failed the schema check")
    if len(str(payload["data"])) > 64_000:
        return Delivery(False, "Payload exceeds the size limit")
    return Delivery(True, "Passed schema, size and request-match checks")


class MockSeller:
    def __init__(self, fail_items: set[str] | None = None):
        self.fail_items = fail_items or set()

    def fetch(self, *, item: str, rail_order_id: str, request_id: str) -> dict[str, Any]:
        if item in self.fail_items:
            raise ConnectionError("seller API returned 502")
        return {"request_id": request_id, "item": item, "data": {"rows": 3, "paid_order": rail_order_id}}


class HttpSeller:
    """Bearer-authenticated controlled seller, invoked only after settlement.

    The configured URL and token belong to the executor; they never come from
    the agent's purchase body. Redirects are refused to keep the bearer local.
    """

    def __init__(self, api_url: str, token: str, client: httpx.Client | None = None):
        if not token.strip():
            raise RuntimeError("MANDATE_SELLER_TOKEN is required for HTTP seller mode")
        url = httpx.URL(api_url)
        if url.scheme not in ("http", "https") or not url.host or url.username or url.password:
            raise ValueError("Seller URL must be an HTTP(S) URL without embedded credentials")
        self._api_url = api_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._owns_client = client is None
        self._http = client or httpx.Client(timeout=5.0)

    def catalog(self) -> list[dict[str, Any]]:
        response = self._http.get(
            f"{self._api_url}/api/v1/catalog", headers=self._headers, follow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise TypeError("Seller catalog must contain an items list")
        for entry in payload["items"]:
            if not isinstance(entry, dict):
                raise TypeError("Seller catalog entries must be objects")
            amount = validate_milli(entry.get("amount_milli"), positive=True)
            if parse_milli(entry.get("amount")) != amount:
                raise ValueError("Seller catalog money units do not match")
        return payload["items"]

    def fetch(self, *, item: str, rail_order_id: str, request_id: str) -> dict[str, Any]:
        response = self._http.post(
            f"{self._api_url}/api/v1/items/{quote(item, safe='')}/fulfill",
            json={"request_id": request_id, "rail_order_id": rail_order_id},
            headers=self._headers, follow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or any(
            payload.get(field) != expected
            for field, expected in (("item", item), ("request_id", request_id), ("rail_order_id", rail_order_id))
        ):
            raise ValueError("Seller response failed purchase correlation")
        if not isinstance(payload.get("created_at"), str):
            raise TypeError("Seller response is missing a timestamp")
        return payload

    def close(self) -> None:
        if self._owns_client:
            self._http.close()


def seller_from_env() -> Seller:
    mode = os.environ.get("MANDATE_SELLER_MODE", "mock").lower()
    if mode == "mock":
        return MockSeller()
    if mode == "http":
        return HttpSeller(
            os.environ.get("MANDATE_SELLER_URL", "http://127.0.0.1:8001"),
            os.environ.get("MANDATE_SELLER_TOKEN", ""),
        )
    raise ValueError("MANDATE_SELLER_MODE must be mock or http")
