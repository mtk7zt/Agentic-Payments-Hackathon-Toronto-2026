"""Production operability for the gateway: request ids, JSON logs, rate limiting, idempotent owner writes.

Everything here is plain ASGI middleware plus two small storage interfaces (`RateLimiter`,
`IdempotencyStore`). Idempotency claims live in the database (`SqlIdempotencyStore`), so every
gateway process shares them; the rate limiter is still per process. A shared limiter replaces
`InMemoryTokenBucket` by implementing the same interface. See docs/OPERATIONS.md.

Nothing in this module logs or stores a header, token, query string or request body.
Credentials only ever appear as a truncated SHA-256 fingerprint used as a bucket key.
"""
from __future__ import annotations

import base64
import contextvars
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Any]
Send = Callable[[Message], Any]

log = logging.getLogger("mandate.ops")

# ───────── request id ─────────

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("mandate_request_id", default="-")


def request_id_from(header: str | None) -> str:
    """The caller's id when it is well formed, otherwise a fresh UUID4. Never trusts free text into logs."""
    if header and REQUEST_ID_RE.fullmatch(header):
        return header
    return str(uuid.uuid4())


def fingerprint(secret: str) -> str:
    """A short one-way key for a credential. Safe to log and to use as a bucket key; never reversible."""
    return hashlib.sha256(secret.encode()).hexdigest()[:16]


# ───────── structured logging ─────────

LOG_FIELDS = ("method", "path", "status", "duration_ms")


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, msg, request_id, method, path, status, duration_ms.

    Fields the record does not carry are null, so every line has the same shape. The request id
    falls back to the context so a log line emitted anywhere inside a request is attributable.
    """

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", None) or request_id_var.get(),
        }
        for field in LOG_FIELDS:
            out[field] = getattr(record, field, None)
        if record.exc_info and record.exc_info[1] is not None:
            # type and message only: no traceback locals, no request state
            out["exc"] = f"{record.exc_info[0].__name__}: {record.exc_info[1]}"
        return json.dumps(out, default=str)


def install_json_logging(name: str = "mandate", stream: Any = None) -> logging.Logger:
    """Attach the JSON formatter to the `mandate` logger tree. Level from MANDATE_LOG_LEVEL (INFO).

    Idempotent: calling it again replaces the handler it installed, so tests can redirect `stream`.
    Propagation is off so uvicorn's own handlers never print a second, unstructured copy.
    """
    logger = logging.getLogger(name)
    level = os.environ.get("MANDATE_LOG_LEVEL", "INFO").strip().upper() or "INFO"
    logger.setLevel(logging.getLevelName(level) if isinstance(logging.getLevelName(level), int) else logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        if getattr(handler, "_mandate_json", False):
            logger.removeHandler(handler)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler._mandate_json = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    return logger


class RequestContextMiddleware:
    """Outermost layer: assigns the request id, echoes it, and writes the access log line.

    It is deliberately outside CORS and the rate limiter so every response, 429s and preflights
    included, carries X-Request-ID and is logged with its real status.
    """

    def __init__(self, app, logger: logging.Logger | None = None) -> None:
        self.app = app
        self.log = logger or logging.getLogger("mandate.access")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        rid = request_id_from(Headers(scope=scope).get("x-request-id"))
        token = request_id_var.set(rid)
        started = time.perf_counter()
        status: int | None = None

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = rid
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            # scope["path"] is the path only: the query string lives in scope["query_string"] and is never logged.
            self.log.info("request", extra={"request_id": rid, "method": scope["method"], "path": scope["path"],
                                            "status": status if status is not None else 500,
                                            "duration_ms": duration_ms})
            request_id_var.reset(token)


# ───────── rate limiting ─────────

DEFAULT_AGENT_PER_MIN = 60
DEFAULT_OWNER_PER_MIN = 300


@dataclass(frozen=True)
class RateLimits:
    agent_per_min: int
    owner_per_min: int
    anon_per_min: int
    enabled: bool = True


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a whole number of requests per minute, not {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{name} cannot be negative (0 disables the limit)")
    return value


def rate_limiting_enabled() -> bool:
    """MANDATE_RATE_LIMIT_ENABLED: on unless it is exactly "0" or "false" (any case). Anything else keeps the limiter on."""
    return os.environ.get("MANDATE_RATE_LIMIT_ENABLED", "1").strip().lower() not in ("0", "false")


def rate_limits_from_env() -> RateLimits:
    """MANDATE_RATE_LIMIT_{AGENT,OWNER,ANON}_PER_MIN. Zero disables that limit. Anonymous defaults to the agent limit."""
    agent = _int_env("MANDATE_RATE_LIMIT_AGENT_PER_MIN", DEFAULT_AGENT_PER_MIN)
    owner = _int_env("MANDATE_RATE_LIMIT_OWNER_PER_MIN", DEFAULT_OWNER_PER_MIN)
    anon = _int_env("MANDATE_RATE_LIMIT_ANON_PER_MIN", agent)
    return RateLimits(agent, owner, anon, rate_limiting_enabled())


def check_rate_limit_config(mode: str) -> RateLimits:
    """Startup check: a malformed limit raises now rather than on the first request; a disabled limiter in
    production is logged as a warning so it cannot be switched off unnoticed."""
    limits = rate_limits_from_env()
    if not limits.enabled and mode == "production":
        log.warning("rate limiting is DISABLED in production (MANDATE_RATE_LIMIT_ENABLED); every caller is unthrottled")
    return limits


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    retry_after: int  # whole seconds until one more request would be allowed; 0 when allowed


class RateLimiter(Protocol):
    """One call per request. `limit` requests per `window_s` for `key`. Implementations must be thread-safe."""

    def acquire(self, key: str, limit: int, window_s: float = 60.0) -> RateDecision: ...

    def reset(self) -> None: ...


class InMemoryTokenBucket:
    """Per-process token bucket: capacity `limit`, refilled continuously at `limit / window_s` per second.

    Idle keys are dropped once the table passes `max_keys`, so a stream of unique client IPs
    cannot grow memory without bound.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic, max_keys: int = 50_000) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last refill time)
        self._lock = threading.Lock()

    def acquire(self, key: str, limit: int, window_s: float = 60.0) -> RateDecision:
        rate = limit / window_s
        with self._lock:
            now = self._clock()
            if len(self._buckets) >= self._max_keys:
                self._prune(now, window_s)
            tokens, last = self._buckets.get(key, (float(limit), now))
            tokens = min(float(limit), tokens + (now - last) * rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                return RateDecision(True, 0)
            self._buckets[key] = (tokens, now)
            return RateDecision(False, max(1, math.ceil((1.0 - tokens) / rate)))

    def _prune(self, now: float, window_s: float) -> None:
        stale = [k for k, (_, last) in self._buckets.items() if now - last >= window_s]
        for k in stale:
            del self._buckets[k]

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._buckets)


def client_ip(scope: Scope) -> str:
    """The socket peer, unless MANDATE_TRUST_PROXY_HEADERS=1 says a proxy in front of us sets X-Forwarded-For."""
    if os.environ.get("MANDATE_TRUST_PROXY_HEADERS", "").strip() in ("1", "true", "yes"):
        forwarded = Headers(scope=scope).get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    client = scope.get("client")
    return client[0] if client else "unknown"


class RateLimitMiddleware:
    """429 + Retry-After once a caller's bucket is empty. Sits inside CORS so the browser can read the 429.

    Keyed, in order of preference, by the owner token fingerprint, the session key fingerprint,
    then the client IP. Probe routes are exempt so orchestrators never get throttled off the box.
    """

    EXEMPT = frozenset({"/live", "/ready", "/health"})

    def __init__(self, app, limiter: RateLimiter, limits: Callable[[], RateLimits] = rate_limits_from_env) -> None:
        self.app = app
        self.limiter = limiter
        self.limits = limits

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self.EXEMPT or scope["method"] == "OPTIONS":
            return await self.app(scope, receive, send)
        limits = self.limits()
        if not limits.enabled:
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        if headers.get("x-owner-token"):
            key, limit = "owner:" + fingerprint(headers["x-owner-token"]), limits.owner_per_min
        elif headers.get("x-session-key"):
            key, limit = "agent:" + fingerprint(headers["x-session-key"]), limits.agent_per_min
        else:
            key, limit = "ip:" + client_ip(scope), limits.anon_per_min
        if limit <= 0:
            return await self.app(scope, receive, send)
        decision = self.limiter.acquire(key, limit)
        if decision.allowed:
            return await self.app(scope, receive, send)
        log.warning("rate limited", extra={"method": scope["method"], "path": scope["path"], "status": 429})
        response = JSONResponse({"detail": "rate limit exceeded", "retry_after": decision.retry_after},
                                status_code=429, headers={"Retry-After": str(decision.retry_after)})
        await response(scope, receive, send)


# ───────── idempotency ─────────

IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
DEFAULT_IDEMPOTENCY_TTL_S = 24 * 3600


def idempotency_ttl_from_env() -> int:
    return _int_env("MANDATE_IDEMPOTENCY_TTL_S", DEFAULT_IDEMPOTENCY_TTL_S)


def body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@dataclass(frozen=True)
class StoredResponse:
    status: int
    headers: list[tuple[bytes, bytes]]
    body: bytes


@dataclass(frozen=True)
class IdempotencyRecord:
    body_sha256: str
    response: StoredResponse


@dataclass(frozen=True)
class Claim:
    """What `IdempotencyStore.claim` decided for one request.

    NEW: this request owns the key and must run, then call `complete` or `release`.
    REPLAY: an earlier request with the same body finished; `record` is its response.
    MISMATCH: the key was first used with a different body.
    IN_FLIGHT: the same key is running right now in this or another process.
    """
    kind: str
    record: IdempotencyRecord | None = None


NEW, REPLAY, MISMATCH, IN_FLIGHT = "NEW", "REPLAY", "MISMATCH", "IN_FLIGHT"
DEFAULT_IN_FLIGHT_LEASE_S = 120  # a claim held longer than this is assumed to belong to a dead process


class IdempotencyStore(Protocol):
    """(key, route) -> one claim at a time. `claim` is atomic: of two concurrent identical requests,
    exactly one gets NEW. Completed responses are kept for `ttl_s` seconds."""

    def claim(self, key: str, route: str, body_sha256: str) -> Claim: ...

    def complete(self, key: str, route: str, response: StoredResponse, ttl_s: int) -> None: ...

    def release(self, key: str, route: str) -> None: ...

    def clear(self) -> None: ...


class InMemoryIdempotencyStore:
    """Per-process store, for tests and single-process sandbox runs. Use `SqlIdempotencyStore`
    whenever more than one gateway process serves the same database."""

    def __init__(self, clock: Callable[[], float] = time.time, lease_s: int = DEFAULT_IN_FLIGHT_LEASE_S) -> None:
        self._clock, self._lease_s = clock, lease_s
        # (key, route) -> (expires_at, body_sha256, record or None while in flight)
        self._rows: dict[tuple[str, str], tuple[float, str, IdempotencyRecord | None]] = {}
        self._lock = threading.Lock()

    def claim(self, key: str, route: str, body_sha256: str) -> Claim:
        now = self._clock()
        with self._lock:
            row = self._rows.get((key, route))
            if row is None or now >= row[0]:
                if len(self._rows) % 256 == 0:  # amortised sweep
                    for k in [k for k, r in self._rows.items() if now >= r[0]]:
                        del self._rows[k]
                self._rows[(key, route)] = (now + self._lease_s, body_sha256, None)
                return Claim(NEW)
            _, digest, record = row
            if digest != body_sha256:
                return Claim(MISMATCH)
            return Claim(REPLAY, record) if record is not None else Claim(IN_FLIGHT)

    def complete(self, key: str, route: str, response: StoredResponse, ttl_s: int) -> None:
        with self._lock:
            row = self._rows.get((key, route))
            if row is not None:
                self._rows[(key, route)] = (self._clock() + ttl_s, row[1], IdempotencyRecord(row[1], response))

    def release(self, key: str, route: str) -> None:
        with self._lock:
            row = self._rows.get((key, route))
            if row is not None and row[2] is None:
                del self._rows[(key, route)]

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()


class SqlIdempotencyStore:
    """Claims kept in the `idempotency_keys` table (migration 6), shared by every gateway process.

    Each step is one statement, so no extra lock is needed: the primary key makes the INSERT claim
    atomic, and taking over an expired row is a conditional UPDATE that only one caller can win.
    """

    def __init__(self, db: Any, clock: Callable[[], float] = time.time,
                 lease_s: int = DEFAULT_IN_FLIGHT_LEASE_S) -> None:
        self.db, self._clock, self._lease_s = db, clock, lease_s

    def claim(self, key: str, route: str, body_sha256: str) -> Claim:
        now = self._clock()
        lease = now + self._lease_s
        won = self.db.execute(
            "INSERT INTO idempotency_keys (key, route, body_sha256, state, expires_at) "
            "VALUES (?, ?, ?, 'IN_FLIGHT', ?) ON CONFLICT (key, route) DO NOTHING",
            (key, route, body_sha256, lease)).rowcount == 1
        if won:
            return Claim(NEW)
        took_over = self.db.execute(
            "UPDATE idempotency_keys SET body_sha256 = ?, state = 'IN_FLIGHT', status = NULL, headers_json = NULL, "
            "body_b64 = NULL, expires_at = ? WHERE key = ? AND route = ? AND expires_at <= ?",
            (body_sha256, lease, key, route, now)).rowcount == 1
        if took_over:
            return Claim(NEW)
        row = self.db.execute(
            "SELECT body_sha256, state, status, headers_json, body_b64 FROM idempotency_keys "
            "WHERE key = ? AND route = ?", (key, route)).fetchone()
        if row is None:  # deleted between our statements; one more attempt settles it
            return self.claim(key, route, body_sha256)
        if row[0] != body_sha256:
            return Claim(MISMATCH)
        if row[1] != "DONE":
            return Claim(IN_FLIGHT)
        headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in json.loads(row[3])]
        response = StoredResponse(int(row[2]), headers, base64.b64decode(row[4]))
        return Claim(REPLAY, IdempotencyRecord(row[0], response))

    def complete(self, key: str, route: str, response: StoredResponse, ttl_s: int) -> None:
        headers = json.dumps([[k.decode("latin-1"), v.decode("latin-1")] for k, v in response.headers])
        self.db.execute(
            "UPDATE idempotency_keys SET state = 'DONE', status = ?, headers_json = ?, body_b64 = ?, expires_at = ? "
            "WHERE key = ? AND route = ? AND state = 'IN_FLIGHT'",
            (response.status, headers, base64.b64encode(response.body).decode("ascii"),
             self._clock() + ttl_s, key, route))

    def release(self, key: str, route: str) -> None:
        self.db.execute("DELETE FROM idempotency_keys WHERE key = ? AND route = ? AND state = 'IN_FLIGHT'",
                        (key, route))

    def purge_expired(self) -> int:
        return self.db.execute("DELETE FROM idempotency_keys WHERE expires_at <= ?", (self._clock(),)).rowcount

    def clear(self) -> None:
        self.db.execute("DELETE FROM idempotency_keys")


class IdempotencyMiddleware:
    """Idempotency-Key on owner POST/PATCH requests (those carrying X-Owner-Token).

    Same key + same body, finished: the stored response, with `Idempotent-Replayed: true`.
    Same key + same body, still running (here or in another process): 409, nothing executed.
    Same key + different body: 422, nothing executed.
    No header, agent requests, GETs: untouched.
    Keys are scoped per owner-token fingerprint and per `METHOD /path`. Only responses below 500
    are stored; a 5xx or a crash releases the key so the same request can be retried.
    """

    METHODS = frozenset({"POST", "PATCH"})

    def __init__(self, app, store: IdempotencyStore, ttl_s: Callable[[], int] = idempotency_ttl_from_env) -> None:
        self.app = app
        self.store = store
        self.ttl_s = ttl_s

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in self.METHODS:
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        idem_key, owner_token = headers.get("idempotency-key"), headers.get("x-owner-token")
        if idem_key is None or not owner_token:
            return await self.app(scope, receive, send)
        if not IDEMPOTENCY_KEY_RE.fullmatch(idem_key):
            return await JSONResponse({"detail": "invalid Idempotency-Key: 1-128 characters from A-Z a-z 0-9 _ . : -"},
                                      status_code=422)(scope, receive, send)

        body = await _read_body(receive)
        route = f"{scope['method']} {scope['path']}"
        key = f"{fingerprint(owner_token)}:{idem_key}"
        claim = await run_in_threadpool(self.store.claim, key, route, body_sha256(body))
        if claim.kind == MISMATCH:
            return await JSONResponse({"detail": "Idempotency-Key was already used with a different body"},
                                      status_code=422)(scope, receive, send)
        if claim.kind == IN_FLIGHT:
            return await JSONResponse({"detail": "a request with this Idempotency-Key is still running; retry shortly"},
                                      status_code=409, headers={"Retry-After": "1"})(scope, receive, send)
        if claim.kind == REPLAY and claim.record is not None:
            return await _replay(claim.record.response, send)

        replayed = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive_again() -> Message:
            return replayed.pop(0) if replayed else await receive()

        status: int | None = None
        raw_headers: list[tuple[bytes, bytes]] = []
        chunks: list[bytes] = []
        complete = False

        async def capture(message: Message) -> None:
            nonlocal status, complete
            if message["type"] == "http.response.start":
                status = message["status"]
                raw_headers.extend(message.get("headers", []))
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                complete = not message.get("more_body", False)
            await send(message)

        stored = False
        try:
            await self.app(scope, receive_again, capture)
            if complete and status is not None and status < 500:
                await run_in_threadpool(self.store.complete, key, route,
                                        StoredResponse(status, raw_headers, b"".join(chunks)), self.ttl_s())
                stored = True
        finally:
            if not stored:
                await run_in_threadpool(self.store.release, key, route)


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


async def _replay(stored: StoredResponse, send: Send) -> None:
    headers = [(k, v) for k, v in stored.headers if k.lower() != b"idempotent-replayed"]
    headers.append((b"idempotent-replayed", b"true"))
    await send({"type": "http.response.start", "status": stored.status, "headers": headers})
    await send({"type": "http.response.body", "body": stored.body, "more_body": False})
