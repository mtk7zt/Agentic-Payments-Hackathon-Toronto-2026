"""Seller delivery check. Runs after settlement and is recorded separately."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


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
