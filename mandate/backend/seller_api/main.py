"""MND-05: controlled synthetic seller endpoints; bearer token required.

Run with only MANDATE_SELLER_TOKEN in this service's environment:
    uvicorn backend.seller_api.main:app --host 127.0.0.1 --port 8001

The executor calls fulfillment after settlement. Order IDs are correlation
references here, not independently verified payment proofs.
"""
from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..models.catalog import CATALOG, catalog_items
from ..models.money import format_milli


class FulfillmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str = Field(min_length=1, max_length=128)
    rail_order_id: str = Field(min_length=1, max_length=128)


def create_app(*, token: str | None = None) -> FastAPI:
    """Deny all requests when no seller token is configured."""
    configured_token = token if token is not None else os.environ.get("MANDATE_SELLER_TOKEN", "")
    app = FastAPI(title="Mandate controlled synthetic seller", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def bearer_required(request: Request, call_next):
        scheme, _, supplied_token = request.headers.get("Authorization", "").partition(" ")
        if (
            not configured_token
            or scheme.lower() != "bearer"
            or not secrets.compare_digest(supplied_token.encode(), configured_token.encode())
        ):
            # Check before JSON decoding/schema errors so unauthorized paths stay hidden.
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        return await call_next(request)

    @app.get("/api/v1/catalog")
    def catalog() -> dict[str, Any]:
        return {"items": catalog_items(FulfillmentIn.model_json_schema())}

    @app.post("/api/v1/items/{item}/fulfill")
    def fulfill(item: str, body: FulfillmentIn) -> dict[str, Any]:
        if item not in CATALOG:
            raise HTTPException(404, "Not Found")
        _, amount = CATALOG[item]
        if item == "shipping-estimate":
            data = {"route": "Toronto to Ottawa", "estimated_days": 2, "shipping_quote_usd": "12.500"}
        else:
            data = {"supplier": "sample-supplier.example", "lead_time_days": 4, "capacity_units": 120}
        return {
            "schema": "mandate.seller-delivery.v1", "synthetic": True, "item": item,
            "request_id": body.request_id, "rail_order_id": body.rail_order_id,
            "amount": format_milli(amount), "amount_milli": amount,
            "created_at": datetime.now(UTC).isoformat(), "data": data,
        }

    return app


app = create_app()
