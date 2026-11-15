"""GOAT Flow x402 request signing.

Follows the published merchant API rules (docs.goat.network, x402 API reference):

1. Collect query parameters and top-level JSON body fields.
2. Convert scalars to strings. Nested values must already be JSON strings.
3. Add api_key, timestamp (Unix seconds) and nonce (fresh per attempt).
4. Drop `sign` and any entry whose value is "". Keep 0 and false.
5. Sort names in ASCII order, join as key=value&key=value. No URL-encoding,
   no method, no path.
6. HMAC-SHA256 with the API secret, lowercase hex.
"""
from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from collections.abc import Mapping

Scalar = str | int | bool


def _to_str(value: Scalar) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    raise TypeError(f"only flat str/int/bool values can be signed, got {type(value).__name__}")


def signing_string(params: Mapping[str, Scalar], api_key: str, timestamp: str, nonce: str) -> str:
    flat = {k: _to_str(v) for k, v in params.items()}
    flat.update({"api_key": api_key, "timestamp": timestamp, "nonce": nonce})
    flat.pop("sign", None)
    kept = {k: v for k, v in flat.items() if v != ""}
    return "&".join(f"{k}={kept[k]}" for k in sorted(kept))


def sign(params: Mapping[str, Scalar], api_key: str, api_secret: str, *, timestamp: str | None = None,
         nonce: str | None = None) -> dict[str, str]:
    """Return the four auth headers. Call again for every attempt, including retries."""
    ts = timestamp or str(int(time.time()))
    nn = nonce or str(uuid.uuid4())
    digest = hmac.new(api_secret.encode(), signing_string(params, api_key, ts, nn).encode(), hashlib.sha256)
    return {"X-API-Key": api_key, "X-Timestamp": ts, "X-Nonce": nn, "X-Sign": digest.hexdigest()}
