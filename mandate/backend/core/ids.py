"""Identifiers that are unique across processes and hosts and sort by creation time.

ULID layout: 48-bit millisecond timestamp + 80 random bits, Crockford base32, 26 characters.
Replaces COUNT(*)+1 numbering, which collides as soon as two gateway processes write.
"""
from __future__ import annotations

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid(now_ms: int | None = None) -> str:
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    value = (ms << 80) | int.from_bytes(os.urandom(10), "big")
    out = []
    for _ in range(26):
        out.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(out))


def new_id(prefix: str) -> str:
    return f"{prefix}_{ulid()}"
