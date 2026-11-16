"""Single-use, bounded buyer loop with discovery/proposal/status tools only.

A model callback receives prior observations and its remaining timeout, then
returns an action object or None to finish. No model SDK or provider credentials
are loaded here. In-flight requests may complete after a timeout; never retry
an uncertain proposal under a new request key.
"""
from __future__ import annotations

import copy
import math
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from queue import Empty, Queue
from threading import Thread
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ..models.money import parse_milli, validate_milli
from ..models.purchase import PurchaseIn

_CALL_DEADLINE: ContextVar[tuple[float, Callable[[], float]] | None] = ContextVar(
    "mandate_call_deadline", default=None,
)


class AgentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal["discover", "propose", "status"]
    arguments: dict[str, Any] = Field(default_factory=dict)


class StatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purchase_id: str = Field(pattern=r"^pur_(?:[0-9]{1,12}|[0-9A-HJKMNP-TV-Z]{26})$")  # legacy numeric or ULID


def _proposal(arguments: dict[str, Any]) -> dict[str, Any]:
    body = PurchaseIn.model_validate(arguments).model_dump()
    validate_milli(parse_milli(body["amount"]), positive=True)
    return body


class AgentGateway:
    """Holds a session capability, never an owner or payment API credential."""

    def __init__(self, api_url: str, session_key: str, client: httpx.Client | None = None):
        url = httpx.URL(api_url)
        if url.scheme not in ("http", "https") or not url.host or url.username or url.password:
            raise ValueError("Gateway URL must be an HTTP(S) URL without embedded credentials")
        if not session_key:
            raise ValueError("An agent session key is required")
        self._url = api_url.rstrip("/")
        self._session_key = session_key
        self._owns_client = client is None
        self._http = client or httpx.Client()

    def _request(self, method: str, path: str, body=None, *, timeout_s: float = 5.0) -> dict[str, Any]:
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise RunDeadline
        local_deadline = time.monotonic() + timeout_s

        def remaining() -> float:
            seconds = local_deadline - time.monotonic()
            bound = _CALL_DEADLINE.get()
            if bound is not None:
                seconds = min(seconds, bound[0] - bound[1]())
            if seconds <= 0:
                raise RunDeadline
            return seconds

        request = self._http.build_request(
            method, self._url + path, json=body, timeout=min(5.0, remaining()),
            headers={"X-Session-Key": self._session_key},
        )
        # Do not inherit owner credentials from an injected/shared HTTP client.
        for header in ("X-Owner-Token", "Authorization", "Cookie"):
            request.headers.pop(header, None)
        # Validation/JSON serialization may consume time too. Recheck the absolute
        # run deadline at the last application boundary before starting HTTP.
        request.extensions["timeout"] = httpx.Timeout(min(5.0, remaining())).as_dict()
        response = self._http.send(request, auth=None, follow_redirects=False)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError("Gateway response must be an object")
        return payload

    def discover(self, *, timeout_s: float = 5.0) -> dict[str, Any]:
        return self._request("GET", "/api/v1/catalog", timeout_s=timeout_s)

    def propose(self, arguments: dict[str, Any], *, timeout_s: float = 5.0) -> dict[str, Any]:
        return self._request("POST", "/api/v1/purchases", _proposal(arguments), timeout_s=timeout_s)

    def status(self, purchase_id: str, *, timeout_s: float = 5.0) -> dict[str, Any]:
        validated = StatusIn(purchase_id=purchase_id)
        return self._request("GET", f"/api/v1/purchases/{validated.purchase_id}", timeout_s=timeout_s)

    def dispatch(self, action: AgentAction, *, timeout_s: float) -> dict[str, Any]:
        if action.tool == "discover":
            if action.arguments:
                raise ValueError("discover does not accept arguments")
            return self.discover(timeout_s=timeout_s)
        if action.tool == "propose":
            return self.propose(action.arguments, timeout_s=timeout_s)
        return self.status(StatusIn.model_validate(action.arguments).purchase_id, timeout_s=timeout_s)

    def close(self) -> None:
        if self._owns_client:
            self._http.close()


@dataclass(frozen=True)
class RunLimits:
    max_steps: int = 20
    timeout_s: float = 60.0
    max_new_attempts: int = 5

    def __post_init__(self):
        if type(self.max_steps) is not int or self.max_steps <= 0:
            raise ValueError("max_steps must be a positive integer")
        if type(self.max_new_attempts) is not int or self.max_new_attempts < 0:
            raise ValueError("max_new_attempts must be a nonnegative integer")
        if (
            isinstance(self.timeout_s, bool) or not isinstance(self.timeout_s, (int, float))
            or not math.isfinite(self.timeout_s) or self.timeout_s <= 0
        ):
            raise ValueError("timeout_s must be positive and finite")


@dataclass
class RunResult:
    reason: str
    steps: int = 0
    new_attempts: int = 0
    observations: list[dict[str, Any]] = field(default_factory=list)
    uncertain_request_key: str | None = None


class RunDeadline(TimeoutError):
    """No more model output or tool dispatch may be accepted for this run."""


def _bounded(call: Callable, deadline: float, clock: Callable[[], float]):
    remaining = deadline - clock()
    if remaining <= 0:
        raise RunDeadline
    result = Queue(maxsize=1)

    def invoke():
        token = _CALL_DEADLINE.set((deadline, clock))
        try:
            if clock() >= deadline:
                raise RunDeadline
            result.put((call(), None))
        except Exception as exc:  # noqa: BLE001 -- Preserve arbitrary callback failures at the worker boundary.
            result.put((None, exc))
        finally:
            _CALL_DEADLINE.reset(token)

    # A late model callback has no access to the gateway and cannot dispatch.
    # A late HTTP request may already have reached the gateway; retain its key.
    Thread(target=invoke, daemon=True, name="mandate-bounded-call").start()
    try:
        value, error = result.get(timeout=remaining)
    except Empty as exc:
        raise RunDeadline from exc
    if clock() >= deadline:
        raise RunDeadline
    if error is not None:
        raise error
    return value


class AgentRunner:
    def __init__(self, gateway: AgentGateway, limits: RunLimits | None = None, *, clock=time.monotonic):
        self._gateway = gateway
        self._limits = limits or RunLimits()
        self._clock = clock
        self._used = False

    def run(self, model: Callable) -> RunResult:
        if self._used:
            raise RuntimeError("AgentRunner is single-use; a task cannot reset its limits")
        self._used = True
        deadline = self._clock() + self._limits.timeout_s
        result = RunResult("step_limit")
        request_keys: set[str] = set()
        for _ in range(self._limits.max_steps):
            if self._clock() >= deadline:
                result.reason = "time_limit"
                return result
            result.steps += 1
            try:
                history = tuple(copy.deepcopy(result.observations))
                raw_action = _bounded(
                    lambda history=history: model(history, deadline - self._clock()), deadline, self._clock,
                )
            except RunDeadline:
                result.reason = "time_limit"
                return result
            except Exception:  # noqa: BLE001 -- A model callback must fail closed without exposing its exception.
                result.reason = "model_error"
                return result
            if raw_action is None:
                result.reason = "finished"
                return result
            try:
                action = AgentAction.model_validate(raw_action)
                request_key = None
                if action.tool == "propose":
                    action.arguments = _proposal(action.arguments)
                    request_key = action.arguments["request_key"]
                    if request_key not in request_keys:
                        if len(request_keys) >= self._limits.max_new_attempts:
                            result.reason = "attempt_limit"
                            return result
                        request_keys.add(request_key)
                        result.new_attempts = len(request_keys)
                elif action.tool == "status":
                    StatusIn.model_validate(action.arguments)
                elif action.arguments:
                    raise ValueError("discover does not accept arguments")
            except (ValueError, TypeError):
                result.reason = "invalid_action"
                return result
            try:
                observation = _bounded(
                    lambda action=action: self._gateway.dispatch(action, timeout_s=deadline - self._clock()),
                    deadline, self._clock,
                )
            except RunDeadline:
                result.reason = "time_limit"
                result.uncertain_request_key = request_key
                return result
            except Exception:  # noqa: BLE001 -- Stop on every tool failure; never retry an uncertain request.
                # Do not expose raw exception messages or retry a possibly accepted proposal.
                result.reason = "tool_error"
                result.uncertain_request_key = request_key
                return result
            result.observations.append({"tool": action.tool, "result": observation})
        return result
