"""Seller delivery check. Runs after settlement and is recorded separately."""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from ..models.catalog import catalog_items
from ..models.money import MoneyError, parse_milli, validate_milli

MAX_DELIVERY_BYTES = 64_000
_DELIVERY_FIELDS = {
    "schema", "synthetic", "request_id", "rail_order_id", "item", "data", "created_at", "amount", "amount_milli",
}


@dataclass(frozen=True)
class Delivery:
    ok: bool
    detail: str


class Seller(Protocol):
    def catalog(self) -> list[dict[str, Any]]: ...
    def fetch(self, *, item: str, rail_order_id: str, request_id: str) -> dict[str, Any]: ...


def verify_delivery(
    payload: dict[str, Any] | None, *, item: str, request_id: str,
    rail_order_id: str | None = None, now: datetime | None = None,
    max_age_s: float = 300.0, future_tolerance_s: float = 30.0,
) -> Delivery:
    """Validate data structure, correlation, byte size and freshness; not truth."""
    if not all(math.isfinite(value) and value >= 0 for value in (max_age_s, future_tolerance_s)):
        raise ValueError("delivery freshness limits must be finite and nonnegative")
    if not isinstance(payload, dict):
        return Delivery(False, "Seller returned no usable payload")
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return Delivery(False, "Payload is not valid JSON data")
    if len(encoded) > MAX_DELIVERY_BYTES:
        return Delivery(False, "Payload exceeds the size limit")
    if set(payload) - _DELIVERY_FIELDS or not isinstance(payload.get("data"), dict):
        return Delivery(False, "Payload failed the schema check")
    if payload.get("schema", "mandate.seller-delivery.v1") != "mandate.seller-delivery.v1":
        return Delivery(False, "Payload has an unsupported schema")
    if "synthetic" in payload and type(payload["synthetic"]) is not bool:
        return Delivery(False, "Payload failed the schema check")
    if payload.get("request_id") != request_id:
        return Delivery(False, "Payload does not match this request")
    if payload.get("item") != item:
        return Delivery(False, "Payload failed the schema check")
    if rail_order_id is not None and payload.get("rail_order_id") != rail_order_id:
        return Delivery(False, "Payload does not match the settled order")
    if "amount" in payload or "amount_milli" in payload:
        try:
            amount = validate_milli(payload.get("amount_milli"), positive=True)
            if parse_milli(payload.get("amount")) != amount:
                return Delivery(False, "Payload money units do not match")
        except MoneyError:
            return Delivery(False, "Payload has invalid money units")
    timestamp = payload.get("created_at")
    if not isinstance(timestamp, str):
        return Delivery(False, "Payload is missing a timestamp")
    try:
        created = datetime.fromisoformat(timestamp)
    except ValueError:
        return Delivery(False, "Payload timestamp is invalid")
    if created.tzinfo is None:
        return Delivery(False, "Payload timestamp must include a timezone")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("delivery validation clock must include a timezone")
    age_s = (current - created).total_seconds()
    if age_s > max_age_s:
        return Delivery(False, "Payload is stale")
    if age_s < -future_tolerance_s:
        return Delivery(False, "Payload timestamp is in the future")
    return Delivery(True, "Passed schema, size, correlation and freshness checks")


class MockSeller:
    def __init__(self, fail_items: set[str] | None = None):
        self.fail_items = fail_items or set()

    def catalog(self) -> list[dict[str, Any]]:
        return catalog_items()

    def fetch(self, *, item: str, rail_order_id: str, request_id: str) -> dict[str, Any]:
        if item in self.fail_items:
            raise ConnectionError("seller API returned 502")
        return {
            "schema": "mandate.seller-delivery.v1", "synthetic": True,
            "request_id": request_id, "rail_order_id": rail_order_id, "item": item,
            "created_at": datetime.now(UTC).isoformat(), "data": {"rows": 3, "paid_order": rail_order_id},
        }


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
        with self._http.stream(
            "POST",
            f"{self._api_url}/api/v1/items/{quote(item, safe='')}/fulfill",
            json={"request_id": request_id, "rail_order_id": rail_order_id},
            headers=self._headers, follow_redirects=False,
        ) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes(chunk_size=8192):
                if len(body) + len(chunk) > MAX_DELIVERY_BYTES:
                    raise ValueError("Seller payload exceeds the size limit")
                body.extend(chunk)
            payload = json.loads(body)
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


def seller_from_env(mode: str | None = None) -> Seller:
    mode = (mode or os.environ.get("MANDATE_SELLER_MODE", "")).lower()
    if mode == "mock":
        return MockSeller()
    if mode == "http":
        return HttpSeller(
            os.environ.get("MANDATE_SELLER_URL", "http://127.0.0.1:8001"),
            os.environ.get("MANDATE_SELLER_TOKEN", ""),
        )
    raise ValueError("MANDATE_SELLER_MODE must be mock or http")
