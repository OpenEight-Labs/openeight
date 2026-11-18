"""Capture admission and completion races use barriers, never timing guesses."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_framework_boundary import fixture
from test_observation_facade import bind as observation_bind

from openeight._content_capture import validate_content_policy
from openeight.langchain._callbacks import ObservationCallbacks
from openeight.langchain._runtime import CURRENT_TOOL_ATTEMPT

bind = observation_bind


def events(owner):
    owner.flush(2)
    return [e for body in owner.client.writes for e in body.get("events", [])]


@pytest.mark.parametrize("expire", [False, True])
def test_duplicate_callbacks_and_end_during_input_capture(bind, expire):
    owner = bind(fixture()[0])
    entered, release = threading.Event(), threading.Event()
    calls = []

    def hook(ctx, value):
        # Other threads can acquire both locks while application code runs.
        with ThreadPoolExecutor() as pool:
            pool.submit(lambda: _touch_locks(invocation, owner)).result(timeout=2)
        calls.append(ctx.direction)
        if ctx.direction == "input":
            entered.set()
            assert release.wait(3)
        return "[REDACTED]"

    owner.content_policy = validate_content_policy("all", hook)
    invocation = owner._admit_invocation({}, "invoke")
    callback, run = ObservationCallbacks(invocation), uuid4()
    with ThreadPoolExecutor() as pool:
        start = pool.submit(
            callback.on_tool_start, {"name": "echo"}, "secret", run_id=run
        )
        assert entered.wait(2)
        callback.on_tool_start({"name": "echo"}, "secret", run_id=run)
        callback.on_tool_end("secret", run_id=run)
        callback.on_tool_end("secret", run_id=run)
        invocation.finish_execution()
        assert not invocation.completed
        assert not events(owner)
        if expire:
            invocation.expire_callbacks()
            assert invocation.completed
        release.set()
        start.result(timeout=3)
    assert calls == ["input", "output"]
    observed = events(owner)
    assert len(observed) == (0 if expire else 1)
    if observed:
        assert json.loads(observed[0]["input_json"]) == "[REDACTED]"
        assert json.loads(observed[0]["output_json"]) == "[REDACTED]"
    assert invocation.completed
    assert owner.worker.reserved_records == 0 and owner.worker.reserved_bytes == 0


def _touch_locks(invocation, owner):
    with invocation.lock, owner.lock, owner.worker.condition:
        return


@pytest.mark.parametrize(
    "signal", [asyncio.CancelledError, KeyboardInterrupt, SystemExit]
)
@pytest.mark.parametrize("direction", ["input", "output"])
def test_callback_cancellation_releases_claims_and_propagates(bind, signal, direction):
    owner = bind(fixture()[0])

    def hook(ctx, value):
        if ctx.direction == direction:
            raise signal
        return value

    owner.content_policy = validate_content_policy("all", hook)
    invocation = owner._admit_invocation({}, "invoke")
    callback, run = ObservationCallbacks(invocation), uuid4()
    with pytest.raises(signal):
        callback.on_tool_start({"name": "echo"}, "secret", run_id=run)
        callback.on_tool_end("secret", run_id=run)
    invocation.finish_execution()
    assert invocation.completed and owner.worker.reserved_records == 0
    assert not events(owner)
    assert not any(owner.delivery_status().content_capture_failures.values())


def test_suppressed_expired_and_disabled_callbacks_never_project(bind):
    owner = bind(fixture()[0])

    def forbidden(*_):
        pytest.fail("ineligible content reached application code")

    owner.content_policy = validate_content_policy("all", forbidden)
    invocation = owner._admit_invocation({}, "invoke")
    callback, run = ObservationCallbacks(invocation), uuid4()
    attempt = SimpleNamespace(
        invocation=invocation, callback_claimed=False, event_id=invocation.run_id
    )
    token = CURRENT_TOOL_ATTEMPT.set(attempt)
    try:
        callback._start_observation("tool_call", run, None, project_input=forbidden)
        callback._finish_observation(run, project_output=forbidden)
    finally:
        CURRENT_TOOL_ATTEMPT.reset(token)
    other = uuid4()
    callback._start_observation("tool_call", other, None)
    invocation.expire_callbacks()
    callback._finish_observation(other, project_output=forbidden)
    invocation.finish_execution()
    callback._start_observation("tool_call", uuid4(), None, project_input=forbidden)
    assert not events(owner)
    assert owner.worker.reserved_records == 0


def test_policy_is_pinned_and_failures_are_safe_immutable_snapshots(bind):
    owner = bind(fixture()[0])

    def broken(*_):
        raise RuntimeError("secret")

    owner.content_policy = validate_content_policy("all", broken)
    invocation = owner._admit_invocation({}, "invoke")
    owner.content_policy = validate_content_policy("none", None)
    callback, run = ObservationCallbacks(invocation), uuid4()
    callback.on_tool_start({"name": "echo"}, "secret", run_id=run)
    first = owner.delivery_status()
    callback.on_tool_end("secret", run_id=run)
    invocation.finish_execution()
    assert first.content_capture_failures["redaction_failed"] == 1
    assert owner.delivery_status().content_capture_failures["redaction_failed"] == 2
    (event,) = events(owner)
    assert event["outcome"] == "success" and "secret" not in json.dumps(event)
    assert "secret" not in repr(owner.delivery_status())


def test_expiry_seals_durable_placeholders_and_new_callback_ids(bind):
    from openeight.langchain._durable_runtime import DurableInvocation

    owner = bind(fixture()[0])
    calls = []
    owner.content_policy = validate_content_policy(
        "all", lambda *_: calls.append("unexpected")
    )
    # Exercise durable completion semantics without a store operation: expiry is synchronous.
    invocation = owner._admit_invocation({}, "invoke")
    invocation.complete_when_settled = DurableInvocation.complete_when_settled.__get__(
        invocation
    )
    callback, parent, child = ObservationCallbacks(invocation), uuid4(), uuid4()
    callback.on_chain_start({}, {}, run_id=child, parent_run_id=parent)
    callback.on_chain_end({}, run_id=child)
    invocation.expire_callbacks()
    callback.on_llm_start({}, ["EXPIRED-SECRET"], run_id=parent)
    callback.on_llm_start({}, ["EXPIRED-SECRET"], run_id=uuid4())
    assert not calls and invocation.entries[parent]["expired"]
    from openeight.langchain._runtime import AgentInvocation

    invocation.complete_when_settled = AgentInvocation.complete_when_settled.__get__(
        invocation
    )
    invocation.finish_execution()
    assert invocation.completed and owner.worker.reserved_records == 0


@pytest.mark.parametrize("governed", [False, True])
def test_error_tool_messages_are_never_optional_output(bind, governed):
    import time

    from langchain_core.messages import ToolMessage

    from openeight.langchain._authorization import OpenEightMiddleware

    owner = bind(fixture()[0])
    calls = []
    owner.content_policy = validate_content_policy(
        "outputs", lambda *_: calls.append("failure-leaked")
    )
    invocation = owner._admit_invocation({}, "invoke")
    result = ToolMessage(
        content="SENSITIVE-FAILURE", status="error", tool_call_id="call"
    )
    if governed:
        attempt = SimpleNamespace(
            invocation=invocation,
            event_id=invocation.run_id,
            decision_id=invocation.run_id,
            contract=SimpleNamespace(
                tool=SimpleNamespace(name="echo", id=invocation.run_id),
                tool_version=SimpleNamespace(id=invocation.run_id),
            ),
            execution_started_at=invocation.started,
            parent=None,
            selected='{"required":"evidence"}',
            started=time.monotonic(),
        )
        OpenEightMiddleware._enqueue_tool_event(None, attempt, False, result)
    else:
        callback, run = ObservationCallbacks(invocation), uuid4()
        callback.on_tool_start({"name": "echo"}, "input", run_id=run)
        callback.on_tool_end(result, run_id=run)
    invocation.finish_execution()
    (event,) = events(owner)
    assert not calls and event["outcome"] == "error"
    assert "output_json" not in event and "SENSITIVE-FAILURE" not in json.dumps(event)
    assert json.loads(event["error_json"]) == {"kind": "tool_error"}
    assert result.content == "SENSITIVE-FAILURE" and result.status == "error"


def test_durable_cancelled_waiter_cannot_advance_tool_order():
    from openeight.langchain._durable_runtime import DurableInvocation

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        order = []

        async def serial(request, handler, *, call_context=None):
            order.append(request)
            if request == "first":
                entered.set()
                await release.wait()
            assert not invocation.execution_lock.locked()
            return request

        invocation = object.__new__(DurableInvocation)
        invocation.owner = SimpleNamespace(_integration="langchain")
        invocation.lock = threading.RLock()
        invocation.native_cancelled = False
        invocation.execution_lock = asyncio.Lock()
        invocation.execution_tail = None
        invocation._tool_call_serial = serial
        first = asyncio.create_task(
            DurableInvocation.tool_call(invocation, "first", None)
        )
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(
            DurableInvocation.tool_call(invocation, "cancelled", None)
        )
        await asyncio.sleep(0)
        third = asyncio.create_task(
            DurableInvocation.tool_call(invocation, "third", None)
        )
        await asyncio.sleep(0)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert order == ["first"] and not third.done()
        release.set()
        assert await first == "first" and await third == "third"
        assert order == ["first", "third"] and invocation.execution_tail.done()

    asyncio.run(exercise())


@pytest.mark.parametrize("redaction_fails", [False, True])
def test_governed_handler_latency_excludes_output_redaction(
    monkeypatch, redaction_fails
):
    from openeight import TelemetryContentContext
    from openeight._content_capture import capture_optional_content
    from openeight.langchain import _authorization
    from openeight.telemetry import uuid7

    clock, captured, reasons = [10.125], [], []

    def redact(context, value):
        clock[0] += 0.25
        if redaction_fails:
            raise ValueError("redaction unavailable")
        return "[REDACTED]"

    policy = validate_content_policy("outputs", redact)

    def capture(kind, direction, project):
        value, reason = capture_optional_content(
            policy, TelemetryContentContext(kind=kind, direction=direction), project
        )
        reasons.append(reason)
        return value

    invocation = SimpleNamespace(
        operation="invoke",
        capture_content=capture,
        enqueue_event=lambda _, event: captured.append(event._as_write()),
    )
    attempt = SimpleNamespace(
        invocation=invocation,
        event_id=uuid7(),
        decision_id=uuid7(),
        parent=None,
        contract=SimpleNamespace(
            tool=SimpleNamespace(name="echo", id=uuid7()),
            tool_version=SimpleNamespace(id=uuid7()),
        ),
        execution_started_at="2026-10-25T00:00:00Z",
        selected='{"required":"evidence"}',
        started=10.0,
    )
    monkeypatch.setattr(
        _authorization, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    _authorization.OpenEightMiddleware._enqueue_tool_event(
        None, attempt, False, "native"
    )
    (event,) = captured
    assert clock[0] == 10.375
    assert event["latency_ms"] == 125
    assert event["outcome"] == "success"
    assert event["input_json"] == attempt.selected
    assert event["output_json"] == ("null" if redaction_fails else '"[REDACTED]"')
    assert reasons == (["redaction_failed"] if redaction_fails else [None])
