"""Fixed synthetic catalog shared by the mock and controlled seller service."""

from typing import Any

from .money import format_milli

CATALOG = {
    "shipping-estimate": ("Synthetic shipping estimate", 40),
    "supplier-summary": ("Synthetic supplier summary", 60),
}


def catalog_items(input_schema: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [
        {
            "item": item, "label": label, "amount": format_milli(amount), "amount_milli": amount,
            "unit": "USD-TDN", "synthetic": True, "input_schema": input_schema or {},
        }
        for item, (label, amount) in CATALOG.items()
    ]
