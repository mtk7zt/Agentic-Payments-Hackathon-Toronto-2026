# Operations

How the gateway behaves in production: request ids, logs, health probes, rate limits and
idempotent owner writes. All of it lives in `backend/api/ops.py` and is wired in
`backend/api/main.py`; no route knows about any of it.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `MANDATE_LOG_LEVEL` | `INFO` | Level for the `mandate.*` loggers (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `MANDATE_RATE_LIMIT_ENABLED` | `1` | Only `0` or `false` turns the limiter off. **Keep it on in production**: a disabled limiter is logged as a `WARNING` at startup in production mode and leaves every caller unthrottled. The test suite sets it to `0` in `conftest.py` because most tests hammer the API and are not about rate limits; the rate-limit tests turn it back on for themselves. |
| `MANDATE_RATE_LIMIT_AGENT_PER_MIN` | `60` | Requests per minute per agent session key. |
| `MANDATE_RATE_LIMIT_OWNER_PER_MIN` | `300` | Requests per minute per owner token. |
| `MANDATE_RATE_LIMIT_ANON_PER_MIN` | same as agent | Requests per minute per client IP for requests that carry neither credential. |
| `MANDATE_TRUST_PROXY_HEADERS` | unset | Set to `1` only when a reverse proxy you control sets `X-Forwarded-For`; the first address in it then keys the anonymous bucket. Unset, the socket peer is used and the header is ignored, so clients cannot pick their own bucket. |
| `MANDATE_IDEMPOTENCY_TTL_S` | `86400` | How long an `Idempotency-Key` record is kept (24 hours). |

A limit of `0` disables that one limit. A limit that is not a whole number makes the process
refuse to start (`ValueError` from `ops.check_rate_limit_config`), so a typo cannot silently
run unthrottled.

Existing settings (`MANDATE_MODE`, `DATABASE_URL`, `MANDATE_OWNER_TOKEN`, `MANDATE_CORS_ORIGINS`, …)
are documented in `backend/config.py`.

## Request ids

Every response carries `X-Request-ID`. If the request already had one matching
`^[A-Za-z0-9-]{8,64}$` it is echoed unchanged, so a caller can correlate its own id across
systems. Anything else (missing, too short, too long, other characters) is replaced by a fresh
UUID4, so free text never reaches the logs through this header.

The id is held in a `contextvars.ContextVar` for the duration of the request. Any log line
written by a `mandate.*` logger while handling that request, including the unhandled-error
line from `CatchUnexpected`, carries it automatically.

`X-Request-ID`, `Retry-After` and `Idempotent-Replayed` are in the CORS `expose_headers` list
so the cockpit can read them in the browser.

## Log format

`ops.install_json_logging()` attaches a JSON formatter to the `mandate` logger tree at startup.
One line per record, always the same eight fields:

```json
{"ts": "2026-11-16T21:58:22.227Z", "level": "INFO", "msg": "request", "request_id": "4817e14b-…", "method": "GET", "path": "/api/v1/state", "status": 429, "duration_ms": 7.88}
```

- `ts` is UTC, millisecond precision, `Z` suffix.
- `msg` is `request` for the access line written once per request by `RequestContextMiddleware`.
  Other records (`rate limited`, `readiness check failed`, `unhandled error on …`) use the same
  shape with `method`/`path`/`status`/`duration_ms` filled in where known and `null` otherwise.
- A record logged with an exception adds `exc` holding the exception type and message only, never
  a traceback with locals.

What is **never** logged: request or response headers, the owner token, session keys, query
strings (the access line uses `scope["path"]`, which excludes the query), request bodies and
response bodies. `backend/tests/test_ops_logging.py` asserts this by issuing requests that carry
a token, a session key, a query string and a body, and checking none of them appear in the
captured output.

Propagation on the `mandate` logger is off, so uvicorn's own handlers do not print a second,
unstructured copy. uvicorn's access log can be silenced with `--no-access-log` since the JSON
access line replaces it.

## Health probes

| Route | Purpose | Response |
| --- | --- | --- |
| `GET /live` | Liveness. The process is up and the ASGI stack answers. Never touches storage. | `200 {"live": true}` |
| `GET /ready` | Readiness. One trivial ledger read (`ledger.policy()` under the ledger lock). | `200 {"ready": true}`, or `503 {"ready": false}` |
| `GET /health` | Unchanged: `{"status", "rail", "mode"}` for humans and the cockpit. | `200` |

`/ready` returns the bare `{"ready": false}` on failure and logs the reason as a `WARNING`
(`readiness check failed`, with `exc`) so a database URL or password in an exception message
never reaches a probe response. The probes need no credentials and are exempt from rate
limiting, so an orchestrator polling them cannot throttle itself off the box.

Suggested Kubernetes wiring: `livenessProbe` on `/live`, `readinessProbe` on `/ready`
(`periodSeconds: 5`, `failureThreshold: 3`). Load balancers should use `/ready`.

## Rate limiting

`RateLimitMiddleware` runs a token bucket per caller. The bucket key is, in order of preference:

1. `owner:<sha256(X-Owner-Token)[:16]>` with the owner limit (300/min by default),
2. `agent:<sha256(X-Session-Key)[:16]>` with the agent limit (60/min),
3. `ip:<client address>` with the anonymous limit (defaults to the agent limit).

Credentials are fingerprinted before they become keys, so the raw token is never stored, and a
wrong token simply gets its own bucket. The bucket holds `limit` tokens and refills continuously
at `limit / 60` per second, so a burst of `limit` requests is allowed and the steady state is
`limit` per minute. Buckets idle for a full window are dropped once the table grows large, so a
stream of unique IPs cannot grow memory without bound.

When the bucket is empty the response is:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 3
Access-Control-Allow-Origin: http://localhost:3000
X-Request-ID: …
{"detail": "rate limit exceeded", "retry_after": 3}
```

`Retry-After` is the whole number of seconds until one more request would be accepted. The
middleware sits *inside* `CORSMiddleware`, so the browser sees the 429 as a real response with
CORS headers rather than a network failure. `OPTIONS` preflights and the three probe routes are
never limited.

## Idempotency

`IdempotencyMiddleware` applies to `POST` and `PATCH` requests that carry both `X-Owner-Token`
and `Idempotency-Key`. Agent routes, `GET`s and requests without the header are untouched.

| Situation | Result |
| --- | --- |
| First request with a key | Executed normally. The response (status, headers, body) is stored under `(owner fingerprint + key, "METHOD /path", sha256(body))` for `MANDATE_IDEMPOTENCY_TTL_S`. |
| Same key, same route, same body | The stored response is returned without touching the ledger, with `Idempotent-Replayed: true`. |
| Same key, same route, different body | `422 {"detail": "Idempotency-Key was already used with a different body"}`. Nothing is executed. |
| Same key, different route | Independent record; the route is part of the key. |
| Malformed key (empty, over 128 chars, characters outside `A-Z a-z 0-9 _ . : -`) | `422` before anything runs. |
| The first attempt ended in a 5xx | Not stored, so a retry with the same key executes again. 4xx responses are stored and replayed, since they are the deterministic answer to that body. |

Keys are scoped by owner-token fingerprint, so two owners (or a rotated token) never collide.
Two concurrent requests with the same key both execute; the last to finish is the one stored.
A shared store that needs exactly-once semantics under concurrency should take a lock or use a
conditional insert in `put`.

## Swapping the in-memory stores for a shared store

Every mechanism is a small interface with one in-memory implementation, constructed once in
`backend/api/main.py`:

```python
rate_limiter: ops.RateLimiter = ops.InMemoryTokenBucket()
idempotency_store: ops.IdempotencyStore = ops.InMemoryIdempotencyStore()
```

Both are per process. Behind more than one worker or replica, a client gets one bucket per
worker (so effectively `N × limit`) and an idempotent replay only hits if the retry lands on the
same worker. To move to Redis or PostgreSQL:

1. Implement the protocol in a new module (for example `backend/core/rate_limit_redis.py`,
   owned by the storage layer):

   ```python
   class RateLimiter(Protocol):
       def acquire(self, key: str, limit: int, window_s: float = 60.0) -> RateDecision: ...
       def reset(self) -> None: ...

   class IdempotencyStore(Protocol):
       def get(self, key: str, route: str) -> IdempotencyRecord | None: ...
       def put(self, key: str, route: str, body_sha256: str, response: StoredResponse, ttl_s: int) -> None: ...
       def clear(self) -> None: ...
   ```

   For Redis, `acquire` is one Lua script doing the refill-and-take atomically (`EVAL` with the
   key, limit, rate and the server clock), and the idempotency record is a hash with `EXPIRE`
   set to `ttl_s`. For PostgreSQL, a `rate_buckets(key, tokens, last)` table updated in one
   `UPDATE … RETURNING`, and an `idempotency(key, route, body_sha256, status, headers, body,
   expires_at)` table with `INSERT … ON CONFLICT`.
2. Change the two constructor lines in `main.py` (they can read a `MANDATE_RATE_LIMIT_URL` /
   `MANDATE_IDEMPOTENCY_URL` and pick the implementation).
3. Run `backend/tests/test_ops_rate_limit.py` and `test_ops_idempotency.py` against the new
   implementation; the middleware tests do not care which store is behind the interface, and
   the unit tests at the bottom of each file show the contract (`clock` injection for TTL and
   refill, `keys()`/`clear()` for inspection).

Nothing under `backend/api/main.py`'s routes changes, and the headers, status codes and log
lines stay identical.

## Running the checks

From `mandate/`:

```
python -m pytest -q
python -m ruff check backend scripts
python scripts/smoke.py
```
