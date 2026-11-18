"""The shared inline wait uses its configured deadline without real sleeping."""

import asyncio
from types import SimpleNamespace

import pytest

from openeight.langchain import ApprovalConfig
from openeight.langchain import _authorization as authorization


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize(
    "timeout,approved_at,expected_wait,expected_block",
    [
        (None, 120.0, 120.0, None),
        (None, None, 3600.0, "approval_wait_timed_out"),
        (30.0, 120.0, 30.0, "approval_wait_timed_out"),
    ],
)
def test_default_wait_and_explicit_override(
    monkeypatch, mode, timeout, approved_at, expected_wait, expected_block
):
    elapsed = 0.0

    def sleep(duration):
        nonlocal elapsed
        elapsed += duration

    async def asleep(duration):
        sleep(duration)

    monkeypatch.setattr(
        authorization, "time", SimpleNamespace(monotonic=lambda: elapsed, sleep=sleep)
    )
    monkeypatch.setattr(authorization, "asyncio", SimpleNamespace(sleep=asleep))
    monkeypatch.setattr(
        authorization, "random", SimpleNamespace(uniform=lambda low, high: 1.0)
    )
    gate = authorization.OpenEightMiddleware(
        agent_name="support",
        endpoint="https://control.example",
        credential="ctk_test",
        approval=None if timeout is None else ApprovalConfig(timeout=timeout),
    )
    assert gate.approval.timeout == (3600.0 if timeout is None else timeout)
    requests = []

    def decide(attempt, **kwargs):
        requests.append(kwargs)
        return SimpleNamespace(
            needs_approval=True, allowed=False, replayed=len(requests) > 1
        ), None

    def poll(result, remaining):
        assert 0 < remaining <= gate.approval.timeout
        return (
            "approved"
            if approved_at is not None and elapsed >= approved_at
            else "pending"
        )

    async def call(function, **kwargs):
        return function()

    monkeypatch.setattr(gate, "_request_decision", decide)
    monkeypatch.setattr(gate, "_poll_approval", poll)
    monkeypatch.setattr(gate, "_enqueue_lifecycle", lambda *args: None)
    gate.owner = SimpleNamespace(_run_client_call=call)
    attempt = SimpleNamespace(waited=False)
    if mode == "sync":
        result, block = gate._authorize_tool_attempt(attempt)
    else:
        result, block = asyncio.run(gate._authorize_tool_attempt_async(attempt))
    assert block == expected_block
    assert attempt.waited
    if expected_block is None:
        assert (
            expected_wait <= elapsed < expected_wait + gate.approval.poll_max_interval
        )
        assert len(requests) == 2 and result.replayed
    else:
        assert elapsed == expected_wait
        assert len(requests) == 1 and not result.replayed
