import asyncio
import json
from uuid import uuid4

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from test_framework_boundary import ToolModel
from test_observation_facade import RecordingClient

from openeight import (
    ConfigurationError,
    DecisionResult,
    FieldSelector,
    PolicyInputRequirement,
    AgentReadiness,
    RegistrationResult,
    Tool,
    ToolReadiness,
    ToolVersion,
)
from openeight.langchain import ApprovalConfig
from openeight.langchain._authorization import OpenEightMiddleware
from openeight.langchain import _owner as implementation


class GovernedClient(RecordingClient):
    verdict = "allow"
    pending = False
    state = "approved"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests = []

    def register_tools(self, facts, **kwargs):
        results = []
        for index, fact in enumerate(facts):
            identity = str(uuid4())
            version = ToolVersion(
                id=str(uuid4()),
                tool_id=identity,
                version="config:test",
                content_digest="sha256:" + "0" * 64,
                input_schema=fact.input_schema,
                tool_contract_digest=None if self.pending else "sha256:" + "1" * 64,
                created_at="2026-10-01T00:00:00Z",
            )
            results.append(
                RegistrationResult(
                    index,
                    "replayed",
                    "",
                    Tool(
                        identity,
                        fact.name,
                        fact.description,
                        "write",
                        True,
                        "test",
                        "echo",
                        "2026-10-01T00:00:00Z",
                    ),
                    version,
                    ToolReadiness(
                        index,
                        fact.name,
                        identity,
                        version.id,
                        "registered",
                        True,
                        not self.pending,
                        not self.pending,
                        "",
                    ),
                )
            )
        self.publications = {item.tool_version.id: item for item in results}
        return tuple(results)

    def get_tool_readiness(self, ids, **kwargs):
        if not ids:
            return AgentReadiness((), True, "registered", True, "ready")
        tools = []
        for identity in ids:
            item = self.publications[identity]
            ready = not self.pending
            requirements = (
                (
                    PolicyInputRequirement(
                        str(uuid4()),
                        str(uuid4()),
                        str(uuid4()),
                        "b" * 64,
                        (FieldSelector("value"),),
                    ),
                )
                if ready
                else ()
            )
            tools.append(
                ToolReadiness(
                    None,
                    item.tool.name,
                    item.tool.id,
                    identity,
                    "registered",
                    True,
                    ready,
                    ready,
                    "ready" if ready else "configuration_required",
                    "a" * 64 if ready else None,
                    requirements,
                )
            )
        return AgentReadiness(
            tuple(tools),
            True,
            "registered",
            ready,
            "ready" if ready else "configuration_required",
        )

    def decide(self, **request):
        self.requests.append(request)
        verdict = self.verdict
        if (
            verdict == "approval_required"
            and len(self.requests) > 1
            and not getattr(self, "stored_approval_verdict", False)
        ):
            verdict = "allow"
        return DecisionResult(
            request["decision_id"],
            verdict,
            "",
            request["run_id"],
            len(self.requests) > 1,
        )

    def approval_state(self, *args, **kwargs):
        return self.state


@pytest.fixture
def make(monkeypatch):
    monkeypatch.setattr(implementation, "Client", GovernedClient)
    owners = []

    def make(middleware=(), candidate=None, on_block="message"):
        effects = []
        if candidate is None:

            @tool
            def echo(value: str) -> str:
                """Echo a value."""
                effects.append(value)
                return value

            candidate = echo
        control = OpenEightMiddleware(
            agent_name="test",
            endpoint="http://localhost:7777",
            credential="ctk_test",
            on_block=on_block,
            approval=ApprovalConfig(
                timeout=0.05, poll_interval=0.001, poll_max_interval=0.002
            ),
        )
        model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call",
                            "name": candidate.name,
                            "args": {"value": "hello"},
                        }
                    ],
                ),
                AIMessage(content="done"),
            ]
        )
        graph = create_agent(
            model, tools=[candidate], middleware=[*middleware, control]
        )
        owner = control.instrument(graph)
        owners.append(owner)
        return owner, graph, control, effects

    yield make
    for owner in owners:
        owner.close(0.2)


def test_allow_exact_selected_attempt_and_bare_graph_rejection(make):
    owner, graph, control, effects = make()
    with pytest.raises(ConfigurationError):
        graph.invoke({"messages": [("user", "x")]})
    assert effects == []
    result = owner.invoke({"messages": [("user", "x")]})
    assert result["messages"][-1].content == "done" and effects == ["hello"]
    assert len(owner.client.requests) == 1
    assert owner.client.requests[0]["input"] == {}
    assert owner.client.requests[0]["selected_values"] == [
        {"path": "value", "value": "hello"}
    ]
    owner.flush(2)
    events = [
        event
        for body in owner.client.writes
        for event in body.get("events", [])
        if event["observation_kind"] == "tool_call"
    ]
    assert (
        len(events) == 1
        and events[0]["decision_id"] == owner.client.requests[0]["decision_id"]
    )
    assert json.loads(events[0]["input_json"]) == {
        key: owner.client.requests[0][key]
        for key in ("input", "selected_values", "policy_contract_set_digest")
    }
    assert "output_json" not in events[0]
    assert control.instrument(graph) is owner


@pytest.mark.parametrize("verdict", ["deny", "dry_run_only"])
def test_blocks_have_zero_effects(make, verdict):
    owner, _, _, effects = make()
    owner.client.verdict = verdict
    owner.invoke({"messages": [("user", "x")]})
    assert effects == []


def test_pending_blocks_before_decide(make):
    owner, _, _, effects = make()
    owner.client.pending = True
    with pytest.raises(ConfigurationError):
        owner.invoke({"messages": [("user", "x")]})
    assert effects == [] and owner.client.requests == []


def test_startup_does_not_replace_contract_refresh_or_fresh_decisions(make):
    owner, _, _, effects = make()
    assert owner.flush(2).catalog_delivery_state == "acknowledged"
    refreshes = []
    register = owner.client.register_tools

    def refresh(facts, **kwargs):
        result = register(facts, **kwargs)
        refreshes.append(result)
        return result

    owner.client.register_tools = refresh
    for _ in range(2):
        owner.invoke({"messages": [("user", "hello")]})
    assert len(refreshes) == 4 and effects == ["hello", "hello"]
    assert len(owner.client.requests) == 2
    assert len({request["decision_id"] for request in owner.client.requests}) == 2
    assert [request["tool_version_id"] for request in owner.client.requests] == [
        result[0].tool_version.id for result in refreshes[1::2]
    ]


def test_inline_approval_replays_identical_bytes(make):
    owner, _, _, effects = make()
    owner.client.verdict = "approval_required"
    owner.invoke({"messages": [("user", "x")]})
    assert effects == ["hello"]
    assert (
        len(owner.client.requests) == 2
        and owner.client.requests[0] == owner.client.requests[1]
    )


def test_inline_lifecycle_retains_target_and_excludes_wait_time(make):
    owner, _, _, effects = make()
    owner.client.verdict = "approval_required"
    owner.invoke({"messages": [("user", "x")]})
    owner.flush(2)
    events = [e for body in owner.client.writes for e in body.get("events", [])]
    lifecycle = [e for e in events if e["observation_kind"] == "lifecycle"]
    assert [e["name"] for e in lifecycle] == ["echo.paused", "echo.resumed"]
    assert [e["outcome"] for e in lifecycle] == ["interrupted", "unspecified"]
    assert all(e.get("parent_event_id") and "latency_ms" not in e for e in lifecycle)
    terminal = next(e for e in events if e["observation_kind"] == "tool_call")
    assert terminal["occurred_at"] >= lifecycle[-1]["occurred_at"]
    assert all(
        e["outcome"] != "interrupted" for e in events if e["observation_kind"] == "span"
    )
    assert effects == ["hello"]


def test_each_outer_retry_creates_new_attempt(make):
    from langchain.agents.middleware import AgentMiddleware

    class Retry(AgentMiddleware):
        def wrap_tool_call(self, request, handler):
            handler(request)
            return handler(request)

    owner, _, _, effects = make([Retry()])
    owner.invoke({"messages": [("user", "x")]})
    assert effects == ["hello", "hello"]
    assert len({request["decision_id"] for request in owner.client.requests}) == 2


def test_async_enforcement_and_retained_cancelled_handler(make):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        effects = []

        @tool
        async def slow(value: str) -> str:
            """Wait for a controlled tool completion."""
            entered.set()
            await release.wait()
            effects.append(value)
            return value

        owner, _, _, _ = make(candidate=slow)
        task = asyncio.create_task(owner.ainvoke({"messages": [("user", "x")]}))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        status = await owner.aclose(0.01)
        assert not status.closed and status.last_error_code == "execution_busy"
        release.set()
        for _ in range(100):
            await asyncio.sleep(0.005)
            if not owner.roots:
                break
        assert effects == ["hello"]
        assert (await owner.aclose(2)).closed

    asyncio.run(exercise())


@pytest.mark.parametrize("state", ["rejected", "expired", "pending"])
def test_unapproved_inline_states_never_execute(make, state):
    owner, _, _, effects = make()
    owner.client.verdict = "approval_required"
    owner.client.state = state
    owner.invoke({"messages": [("user", "x")]})
    assert effects == [] and len(owner.client.requests) == 1


def test_enforcement_rejects_control_before_later_middleware(make):
    from langchain.agents.middleware import AgentMiddleware

    class Later(AgentMiddleware):
        def wrap_tool_call(self, request, handler):
            return handler(request)

        async def awrap_tool_call(self, request, handler):
            return await handler(request)

    owner, _, control, _ = make()
    graph = create_agent(
        ToolModel(responses=[AIMessage(content="done")]),
        tools=list(owner.catalog.entries[name].tool for name in owner.catalog.entries),
        middleware=[OpenEightMiddleware(**{k: v for k, v in control._binding.items() if k != "integration"}), Later()],
    )
    with pytest.raises(ConfigurationError):
        graph_control = graph.builder.nodes[
            "OpenEightMiddleware.before_agent"
        ].runnable.func.__self__
        graph_control.instrument(graph)


def test_cancelled_executor_backed_sync_tool_retains_ownership(make):
    import threading

    async def exercise():
        entered, release = threading.Event(), threading.Event()
        effects = []

        @tool
        def slow(value: str) -> str:
            """Wait in a framework executor."""
            entered.set()
            release.wait(2)
            effects.append(value)
            return value

        owner, _, _, _ = make(candidate=slow)
        task = asyncio.create_task(owner.ainvoke({"messages": [("user", "x")]}))
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not (await owner.aclose(0.01)).closed
        release.set()
        for _ in range(100):
            if not owner.roots:
                break
            await asyncio.sleep(0.005)
        assert effects == ["hello"]
        assert (await owner.aclose(2)).closed

    asyncio.run(exercise())


def test_approved_stored_verdict_requires_successful_exact_replay(make):
    owner, _, _, effects = make()
    owner.client.verdict = "approval_required"
    owner.client.stored_approval_verdict = True
    owner.invoke({"messages": [("user", "x")]})
    assert effects == ["hello"]
    assert owner.client.requests[0] == owner.client.requests[1]


def test_sync_authorized_handler_remains_owned_during_concurrent_close(make):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    entered, release = threading.Event(), threading.Event()
    effects = []

    @tool
    def slow(value: str) -> str:
        """Hold a synchronous external effect."""
        entered.set()
        release.wait(2)
        effects.append(value)
        return value

    owner, _, _, _ = make(candidate=slow)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(owner.invoke, {"messages": [("user", "x")]})
        assert entered.wait(1)
        try:
            status = owner.close(0.01)
            assert not status.closed and status.last_error_code == "execution_busy"
        finally:
            release.set()
        result.result(2)
    assert effects == ["hello"]
    assert owner.close(2).closed


def test_close_seals_handler_admission_after_decide(make):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    authorized, release = threading.Event(), threading.Event()
    owner, _, _, effects = make()
    decide = owner.client.decide

    def blocked_decide(**request):
        result = decide(**request)
        authorized.set()
        release.wait(2)
        return result

    owner.client.decide = blocked_decide
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(owner.invoke, {"messages": [("user", "x")]})
        assert authorized.wait(1)
        owner.close(0.01)
        release.set()
        with pytest.raises(ConfigurationError):
            result.result(2)
    assert effects == []


def test_retained_handler_timestamp_is_published_before_completion(make, monkeypatch):
    import threading
    from types import SimpleNamespace

    from openeight.langchain import _authorization as middleware

    owner, _, control, _ = make()
    invocation = owner._admit_invocation({}, "invoke")
    invocation.handlers = 1
    invocation.finish_execution()
    entered, released, settled = threading.Event(), threading.Event(), threading.Event()
    timestamp = "2026-10-02T00:00:00Z"

    def now():
        entered.set()
        assert released.wait(2)
        return timestamp

    monkeypatch.setattr(middleware, "utc_timestamp", now)
    thread = threading.Thread(
        target=control._settle_handler, args=(SimpleNamespace(invocation=invocation),)
    )
    thread.start()
    assert entered.wait(2)
    competitor = threading.Thread(
        target=lambda: (invocation.complete_when_settled(), settled.set())
    )
    competitor.start()
    assert not settled.wait(0.02)
    released.set()
    thread.join(2)
    competitor.join(2)
    assert invocation.completed and invocation.ended == timestamp
    assert owner.flush(2).pending_records == 0
    completed = [
        run
        for write in owner.client.writes
        for run in write.get("runs", [])
        if run["status"] == "completed"
    ]
    assert completed[0]["ended_at"] == timestamp


@pytest.mark.parametrize(
    "failure", ["foreign_decision", "foreign_run", "unavailable", "stale"]
)
def test_decision_uncertainty_never_executes_handler(make, failure):
    from openeight import APIError, ProtocolError, TransportError

    owner, _, _, effects = make()

    def decide(**request):
        if failure == "unavailable":
            raise TransportError()
        if failure == "stale":
            raise APIError(409, "conflict", "AUTHORIZATION_STALE")
        return DecisionResult(
            str(uuid4()) if failure == "foreign_decision" else request["decision_id"],
            "allow",
            "",
            str(uuid4()) if failure == "foreign_run" else request["run_id"],
            False,
        )

    owner.client.decide = decide
    if failure == "stale":
        owner.invoke({"messages": [("user", "x")]})
    else:
        with pytest.raises((ProtocolError, TransportError)):
            owner.invoke({"messages": [("user", "x")]})
    assert effects == []


def test_mutated_bound_tool_is_blocked_before_decide(make):
    owner, _, _, effects = make()
    owner.catalog.entries["echo"].tool.name = "changed"
    with pytest.raises((ConfigurationError, ValueError)):
        owner.invoke({"messages": [("user", "x")]})
    assert effects == [] and owner.client.requests == []


def test_approved_state_without_valid_replay_stays_blocked(make):
    owner, _, _, effects = make()

    def decide(**request):
        return DecisionResult(
            request["decision_id"], "approval_required", "", request["run_id"], False
        )

    owner.client.decide = decide
    owner.invoke({"messages": [("user", "x")]})
    assert effects == []


def test_repeated_cancellation_drains_registration_and_releases_root(make):
    import threading

    async def check():
        owner, _, _, effects = make()
        entered, release = threading.Event(), threading.Event()
        original = owner.client.register_tools

        def register(*args, **kwargs):
            entered.set()
            assert release.wait(2)
            return original(*args, **kwargs)

        owner.client.register_tools = register
        task = asyncio.create_task(owner.ainvoke({"messages": [("user", "x")]}))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set()
            task.cancel()
            await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert effects == []
        assert (await owner.aclose(2)).closed
        assert not owner.roots

    asyncio.run(check())


def read_registration(register, *, ready=True):
    from dataclasses import replace

    def publish(facts, **kwargs):
        return tuple(
            replace(
                row,
                tool=replace(row.tool, capability="read"),
                readiness=replace(
                    row.readiness,
                    eligible=ready,
                    policy_ready=ready,
                    reason="read_only" if ready else "classification_required",
                ),
            )
            for row in register(facts, **kwargs)
        )

    return publish


@pytest.mark.parametrize("async_call", [False, True])
def test_read_tool_executes_without_decision_and_retains_observation(make, async_call):
    owner, _, _, effects = make()
    owner.client.register_tools = read_registration(owner.client.register_tools)
    if async_call:
        asyncio.run(owner.ainvoke({"messages": [("user", "read")]}))
    else:
        owner.invoke({"messages": [("user", "read")]})
    assert effects == ["hello"]
    assert owner.client.requests == []
    assert owner.flush(2).pending_records == 0
    events = [event for body in owner.client.writes for event in body.get("events", [])]
    reads = [event for event in events if event.get("observation_kind") == "tool_call"]
    assert len(reads) == 1
    assert reads[0].get("tool_id") and reads[0].get("tool_version_id")
    assert not reads[0].get("decision_id")
    assert "input_json" not in reads[0] and "output_json" not in reads[0]


def test_read_dispatch_requires_fresh_readiness(make):
    owner, _, _, effects = make(on_block="raise")
    owner.client.register_tools = read_registration(
        owner.client.register_tools, ready=False
    )
    with pytest.raises(ConfigurationError):
        owner.invoke({"messages": [("user", "read")]})
    assert effects == [] and owner.client.requests == []


def test_read_to_write_override_is_observed_before_dispatch(make):
    owner, _, _, effects = make()
    original = owner.client.register_tools
    count = 0

    def refresh(facts, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            return read_registration(original)(facts, **kwargs)
        return original(facts, **kwargs)

    owner.client.register_tools = refresh
    owner.invoke({"messages": [("user", "write now")]})
    assert effects == ["hello"]
    assert len(owner.client.requests) == 1

@pytest.mark.parametrize("stage", ["readiness", "read_dispatch", "decision", "approval_replay"])
@pytest.mark.parametrize("reason", ["TRIAL_EXPIRED", "DECISION_LIMIT_REACHED", "ACCESS_UNAVAILABLE"])
def test_commercial_refusal_never_dispatches_native_handler(make, stage, reason):
    from openeight import APIError

    owner, _, _, effects = make()
    calls = []
    def refuse(*args, **kwargs):
        calls.append(reason)
        raise APIError(503 if reason == "ACCESS_UNAVAILABLE" else 403,
                       "unavailable" if reason == "ACCESS_UNAVAILABLE" else "permission_denied", reason)
    if stage in {"readiness", "read_dispatch"}:
        if stage == "read_dispatch":
            owner.client.register_tools = read_registration(owner.client.register_tools)
        owner.client.get_tool_readiness = refuse
    elif stage == "decision":
        owner.client.decide = refuse
    else:
        original = owner.client.decide
        owner.client.verdict = "approval_required"
        def replay_refusal(*args, **kwargs):
            if owner.client.requests:
                return refuse(*args, **kwargs)
            return original(*args, **kwargs)
        owner.client.decide = replay_refusal
    try:
        owner.invoke({"messages": [("user", "x")]})
    except APIError:
        pass
    assert calls and set(calls) == {reason}
    if stage not in {"readiness", "read_dispatch"}:
        assert len(calls) == 1
    assert effects == []
