"""Public proposal input shared by the gateway and agent HTTP client."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PurchaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_key: str = Field(min_length=8, max_length=128)
    seller_domain: str = Field(min_length=1, max_length=253)
    seller_address: str = Field(min_length=1, max_length=128)
    item: str = Field(min_length=1, max_length=128)
    amount: str = Field(description="Decimal USD-TDN string with at most three fractional digits")
    tool_call: dict[str, Any] = Field(default_factory=dict)
