"""MND-06 bounded runner, with deterministic models and no model-provider keys."""

import json
import threading
import time

import httpx
import pytest

from backend.agent import runner as runner_module
from backend.agent.runner import AgentGateway, AgentRunner, RunLimits


def proposal(key):
    return {"tool": "propose", "arguments": {
        "request_key": key, "seller_domain": "seller-a.example", "seller_address": "0xa1",
        "item": "shipping-estimate", "amount": "0.040", "tool_call": {"tool": "buy_supplier_data"},
    }}


class Gateway:
    def __init__(self):
        self.calls = []

    def dispatch(self, action, *, timeout_s):
        self.calls.append(action)
        return {"stage": "VERIFIED", "id": "pur_0001"}


def test_model_step_limit_bounds_repeated_read_tools():
    gateway = Gateway()
    result = AgentRunner(gateway, RunLimits(max_steps=3)).run(lambda history, timeout: {"tool": "discover"})
    assert result.reason == "step_limit" and result.steps == 3
    assert result.new_attempts == 0 and len(gateway.calls) == 3


def test_new_attempt_limit_stops_before_extra_proposal():
    gateway = Gateway()
    actions = iter((proposal("request-0001"), proposal("request-0001"), proposal("request-0002"), proposal("request-0003")))
    result = AgentRunner(gateway, RunLimits(max_new_attempts=2)).run(lambda history, timeout: next(actions))
    assert result.reason == "attempt_limit" and result.new_attempts == 2
    assert len(gateway.calls) == 3  # replay counts as a step, not a new request key


@pytest.mark.parametrize("tool", ["approve", "reconcile", "set_policy", "shell", "checkout"])
def test_unexposed_tools_cannot_be_dispatched(tool):
    gateway = Gateway()
    result = AgentRunner(gateway).run(lambda history, timeout: {"tool": tool, "arguments": {}})
    assert result.reason == "invalid_action" and gateway.calls == []


def test_slow_model_has_no_late_tool_dispatch():
    gateway = Gateway()
    release = threading.Event()
    finished = threading.Event()

    def model(history, timeout):
        try:
            release.wait(5)
            return proposal("late-request")
        finally:
            finished.set()

    start = time.monotonic()
    try:
        result = AgentRunner(gateway, RunLimits(timeout_s=0.1)).run(model)
        assert result.reason == "time_limit" and gateway.calls == []
        assert time.monotonic() - start < 2
    finally:
        release.set()
        assert finished.wait(2)


def test_expired_model_result_cannot_dispatch_with_injected_clock():
    gateway = Gateway()
    clock = [0.0]

    def model(history, timeout):
        assert timeout == 1
        clock[0] = 2
        return proposal("expired-request")

    result = AgentRunner(gateway, RunLimits(timeout_s=1), clock=lambda: clock[0]).run(model)
    assert result.reason == "time_limit" and gateway.calls == []


def test_finished_run_cannot_be_restarted_to_reset_its_limits():
    runner = AgentRunner(Gateway())
    assert runner.run(lambda history, timeout: None).reason == "finished"
    with pytest.raises(RuntimeError, match="single-use"):
        runner.run(lambda history, timeout: None)


@pytest.mark.parametrize("limits", [{"max_steps": 0}, {"max_new_attempts": True}, {"timeout_s": float("inf")}])
def test_run_limits_are_explicit_and_validated(limits):
    with pytest.raises(ValueError):
        RunLimits(**limits)


def test_agent_http_client_sends_only_session_credentials_and_exact_money():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "pur_0001", "stage": "VERIFIED"})

    with httpx.Client(
        transport=httpx.MockTransport(handler), headers={"X-Owner-Token": "owner-secret"}, auth=("user", "secret"),
    ) as client:
        gateway = AgentGateway("https://gateway.example", "agent-session", client)
        gateway.discover()
        gateway.propose(proposal("request-0001")["arguments"])
        gateway.status("pur_0001")
    assert [request.method for request in seen] == ["GET", "POST", "GET"]
    assert all(request.headers["X-Session-Key"] == "agent-session" for request in seen)
    assert all("X-Owner-Token" not in request.headers and "Authorization" not in request.headers for request in seen)
    assert json.loads(seen[1].content)["amount"] == "0.040"


def test_gateway_timeout_retains_original_request_reference_without_retry():
    seen = []

    def timeout(request):
        seen.append(request)
        raise httpx.ReadTimeout("ambiguous server outcome")

    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        runner = AgentRunner(AgentGateway("https://gateway.example", "session", client))
        result = runner.run(lambda history, remaining: proposal("original-request"))
    assert result.reason == "tool_error" and result.uncertain_request_key == "original-request"
    assert len(seen) == 1


def test_slow_tool_retains_key_and_never_dispatches_a_second_request():
    release = threading.Event()
    finished = threading.Event()

    class SlowGateway(Gateway):
        def dispatch(self, action, *, timeout_s):
            self.calls.append(action)
            try:
                release.wait(5)
                return {"stage": "VERIFIED"}
            finally:
                finished.set()

    gateway = SlowGateway()
    try:
        result = AgentRunner(gateway, RunLimits(timeout_s=0.2)).run(lambda history, timeout: proposal("slow-request"))
        assert result.reason == "time_limit" and result.uncertain_request_key == "slow-request"
        assert result.new_attempts == 1 and len(gateway.calls) == 1
    finally:
        release.set()
        assert finished.wait(2)


def test_delayed_tool_worker_cannot_send_after_runner_returns(monkeypatch):
    original_thread = runner_module.Thread
    release = threading.Event()
    finished = threading.Event()
    created = [0]
    seen = []

    def delayed_thread(*args, **kwargs):
        created[0] += 1
        if created[0] == 2:
            target = kwargs["target"]

            def delayed():
                try:
                    release.wait(5)
                    target()
                finally:
                    finished.set()

            kwargs["target"] = delayed
        return original_thread(*args, **kwargs)

    monkeypatch.setattr(runner_module, "Thread", delayed_thread)

    def transport(request):
        seen.append(request)
        return httpx.Response(200, json={"stage": "VERIFIED"})

    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        try:
            result = AgentRunner(AgentGateway("http://gateway.test", "session", http), RunLimits(timeout_s=0.1)).run(
                lambda history, remaining: proposal("delayed-request"),
            )
            assert result.reason == "time_limit" and seen == []
            release.set()
            assert finished.wait(2)
            assert seen == []
        finally:
            release.set()
            finished.wait(2)


def test_request_preparation_cannot_consume_deadline_then_send(monkeypatch):
    clock = [0.0]
    seen = []
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: seen.append(request) or httpx.Response(200, json={"stage": "VERIFIED"}),
    )) as http:
        build = http.build_request

        def slow_build(*args, **kwargs):
            request = build(*args, **kwargs)
            clock[0] = 2.0
            return request

        monkeypatch.setattr(http, "build_request", slow_build)
        result = AgentRunner(
            AgentGateway("http://gateway.test", "session", http), RunLimits(timeout_s=1), clock=lambda: clock[0],
        ).run(lambda history, remaining: proposal("prepare-request"))
    assert result.reason == "time_limit" and seen == []


def test_direct_gateway_refuses_expired_timeout_before_http():
    seen = []
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: seen.append(request) or httpx.Response(200, json={}),
    )) as http:
        gateway = AgentGateway("http://gateway.test", "session", http)
        with pytest.raises(TimeoutError):
            gateway.discover(timeout_s=0)
    assert seen == []
