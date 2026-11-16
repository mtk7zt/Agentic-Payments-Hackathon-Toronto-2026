"""Read-only SSE framing and short-lived, task-scoped browser grants."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator, Callable

from fastapi import Request

from ..core.ledger import Ledger

GRANT_SECONDS = 60


def mint_grant(secret: str, mandate: str, agent: str, *, now: float | None = None) -> str:
    body = json.dumps({"aud": "mandate.stream", "mandate": mandate, "agent": agent,
                       "exp": int(time.time() if now is None else now) + GRANT_SECONDS},
                      sort_keys=True, separators=(",", ":")).encode()
    payload = base64.urlsafe_b64encode(body).decode().rstrip("=")
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def read_grant(token: str, secret: str, mandate: str, agent: str, *, now: float | None = None) -> int | None:
    if not secret or len(token) > 2048:
        return None
    try:
        payload, signature = token.split(".")
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return None
        body = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        clock = time.time() if now is None else now
        if (body["aud"] != "mandate.stream" or body["mandate"] != mandate or body["agent"] != agent
                or type(body["exp"]) is not int or not clock < body["exp"] <= clock + GRANT_SECONDS):
            return None
        return body["exp"]
    except (ValueError, KeyError, TypeError, UnicodeError):
        return None


async def stream_frames(request: Request, ledger: Ledger, cursor: int,
                        valid: Callable[[], bool]) -> AsyncIterator[str]:
    yield "retry: 2000\n\n"
    idle = 0
    while not await request.is_disconnected():
        if not valid():
            yield 'event: session.expired\ndata: {}\n\n'
            return
        # Snapshot reads only. Financial state is never changed by a subscriber.
        events = ledger.events()
        head = events[-1]["seq"] if events else 0
        fresh = [event for event in events if event["seq"] > cursor]
        if cursor > head or (fresh and fresh[0]["seq"] != cursor + 1):
            yield 'event: stream.reset\ndata: {"reason":"cursor_gap","refresh":true}\n\n'
            return
        for event in fresh:
            if not valid():
                yield 'event: session.expired\ndata: {}\n\n'
                return
            if event["seq"] != cursor + 1:
                yield 'event: stream.reset\ndata: {"reason":"cursor_gap","refresh":true}\n\n'
                return
            cursor = event["seq"]
            yield f"id: {cursor}\nevent: ledger.appended\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
        idle = 0 if fresh else idle + 1
        if idle >= 30:
            idle = 0
            yield ": keep-alive\n\n"
        await asyncio.sleep(0.5)
