"""Native Deep Agents execution with shared SDK authorization and evidence."""

import asyncio

import pytest

pytest.importorskip("deepagents")

from _policy_helpers import PolicyClient, call, config, model
from deepagents.middleware.skills import SkillsMiddleware
from langchain.agents.middleware import AgentMiddleware
from langchain_core.globals import get_debug, set_debug
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from test_framework_boundary import ToolModel

from openeight import CleanupIncompleteError, ConfigurationError
from openeight.deepagents import create_deep_agent, observe
from openeight.langchain import _owner as implementation
from openeight.langchain._bindings import mapped_name

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def client(monkeypatch):
    PolicyClient.instances = []
    monkeypatch.setattr(implementation, "Client", PolicyClient)


def effect(effects):
    @tool
    async def effect(value: str) -> str:
        """Record an effect."""
        effects.append(value)
        return value

    return effect


async def test_entry_publishes_effective_catalog_without_execution():
    context = create_deep_agent(model(), openeight=config())
    assert not PolicyClient.instances
    async with context as agent:
        await agent.aflush()
        client = PolicyClient.instances[0]
        rows = agent.catalog_snapshot().bindings
        assert {(), ("general-purpose",)} == {row["child_path"] for row in rows}
        assert any(row["native_name"] == "write_file" for row in rows)
        assert client.registrations and not client.requests and not client.writes
    assert client.closed
    assert "ctk_secret" not in repr(config())


@pytest.mark.parametrize("path", [(), ("general-purpose",), ("researcher",)])
@pytest.mark.parametrize("deny", [False, True])
async def test_root_and_child_decisions_have_distinct_catalog_identity(path, deny):
    effects = []
    tool = effect(effects)
    child = (
        dict(name=path[0], description="work", model=model(call()), tools=[tool])
        if path
        else None
    )
    parent = (
        model(call("task", description="work", subagent_type=path[0]))
        if path
        else model(call())
    )
    async with create_deep_agent(
        parent, tools=[tool], subagents=[child] if child else [], openeight=config()
    ) as agent:
        client = PolicyClient.instances[0]
        if deny:
            client.reject_name = mapped_name(path, "effect")
        result = await agent.ainvoke({"messages": [("user", "go")]})
        assert result["messages"][-1].content == "done"
        assert effects == ([] if deny else ["original"])
        attempts = [
            r
            for r in client.requests
            if client.contract_names[r["tool_id"]] == mapped_name(path, "effect")
        ]
        assert (
            len(attempts) == 1
            and attempts[0]["input"] == {}
            and attempts[0]["selected_values"]
            == [{"path": "value", "value": "original"}]
        )
        assert len({r["run_id"] for r in client.requests}) == 1
        await agent.aflush()
        events = [event for batch in client.writes for event in batch.get("events", [])]
        executions = [
            event
            for event in events
            if event.get("tool_name") == mapped_name(path, "effect")
        ]
        assert len(executions) == (0 if deny else 1)
        if executions:
            assert executions[0]["decision_id"] == attempts[0]["decision_id"]


@pytest.mark.parametrize("path", [(), ("general-purpose",), ("researcher",)])
async def test_native_approval_edits_are_authorized_before_execution(tmp_path, path):
    effects = []
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "state.db")) as saver:
        children = (
            [
                dict(
                    name=path[0],
                    description="work",
                    model=model(call()),
                    tools=[effect(effects)],
                    interrupt_on={"effect": True},
                )
            ]
            if path
            else []
        )
        parent = (
            model(call("task", description="work", subagent_type=path[0]))
            if path
            else model(call())
        )
        async with create_deep_agent(
            parent,
            tools=[effect(effects)],
            subagents=children,
            interrupt_on={"effect": True} if not path else None,
            checkpointer=saver,
            openeight=config(),
        ) as agent:
            cfg = {"configurable": {"thread_id": agent.new_thread()}}
            interrupted = await agent.ainvoke({"messages": [("user", "go")]}, cfg)
            assert interrupted.get("__interrupt__") and not effects
            await agent.ainvoke(
                Command(
                    resume={
                        "decisions": [
                            {
                                "type": "edit",
                                "edited_action": {
                                    "name": "effect",
                                    "args": {"value": "edited"},
                                },
                            }
                        ]
                    }
                ),
                cfg,
            )
            assert effects == ["edited"]
            client = PolicyClient.instances[0]
            requests = [
                r
                for r in client.requests
                if r["selected_values"] == [{"path": "value", "value": "edited"}]
            ]
            assert len(requests) == 1 and client.readiness_calls == (6 if path else 4)


async def test_callable_mutation_rejects_before_model():
    effects = []
    candidate = effect(effects)
    async with create_deep_agent(
        model(call()), tools=[candidate], openeight=config()
    ) as agent:
        candidate.coroutine = effect([]).coroutine
        with pytest.raises(ConfigurationError, match="binding"):
            await agent.ainvoke({"messages": [("user", "go")]})
        assert not effects and not PolicyClient.instances[0].requests


async def test_mutation_during_decide_cannot_execute(monkeypatch):
    effects = []
    candidate = effect(effects)
    async with create_deep_agent(
        model(call()), tools=[candidate], openeight=config()
    ) as agent:
        client = PolicyClient.instances[0]
        decide = client.decide

        def mutate(**request):
            candidate.coroutine = effect([]).coroutine
            return decide(**request)

        monkeypatch.setattr(client, "decide", mutate)
        with pytest.raises(ConfigurationError):
            await agent.ainvoke({"messages": [("user", "go")]})
        assert not effects


async def test_foreign_checkpoint_id_never_reaches_saver(tmp_path):
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "state.db")) as saver:
        async with create_deep_agent(
            model(), openeight=config(), checkpointer=saver
        ) as first:
            thread = first.new_thread()
        async with create_deep_agent(
            model(), openeight=config(), checkpointer=saver
        ) as second:
            await second.aflush()
            readiness_before = PolicyClient.instances[-1].readiness_calls
            with pytest.raises(ConfigurationError) as error:
                await second.ainvoke(
                    {"messages": []}, {"configurable": {"thread_id": thread}}
                )
            assert error.value.reason == "checkpoint_thread_unowned"
            assert PolicyClient.instances[-1].readiness_calls == readiness_before


async def test_observation_is_explicit_and_does_not_decide():
    effects = []
    async with observe(
        model(call()),
        tools=[effect(effects)],
        openeight={
            "agent_name": "test",
            "endpoint": "http://localhost:7777",
            "credential": "ctk_test",
        },
    ) as agent:
        await agent.ainvoke({"messages": [("user", "go")]})
        assert effects == ["original"] and not PolicyClient.instances[0].requests


async def test_unsupported_skill_resolver_rejects_before_client():
    with pytest.raises(ConfigurationError):
        create_deep_agent(
            model(),
            middleware=[
                SkillsMiddleware(backend=None, sources=[], tools=lambda *a: [])
            ],
            openeight=config(),
        )
    assert not PolicyClient.instances


async def test_debug_rejects_without_printing_state(capsys):
    prior = get_debug()
    try:
        set_debug(True)
        with pytest.raises(ConfigurationError):
            create_deep_agent(model(), openeight=config())
    finally:
        set_debug(prior)
    assert not PolicyClient.instances and capsys.readouterr().out == ""


async def test_unconfigured_returns_block_message():
    effects = []
    async with create_deep_agent(
        model(call()), tools=[effect(effects)], openeight=config()
    ) as agent:
        PolicyClient.instances[0].pending = True
        await agent.ainvoke({"messages": [("user", "go")]})
        assert not effects and not PolicyClient.instances[0].requests


async def test_parallel_children_share_run_and_settle_each_effect_once():
    effects = []
    root = AIMessage(
        content="",
        tool_calls=[
            dict(
                name="task",
                args={"description": "work", "subagent_type": name},
                id=name,
            )
            for name in ("a", "b")
        ],
    )
    children = [
        dict(
            name=name,
            description="work",
            model=model(call(value=name)),
            tools=[effect(effects)],
        )
        for name in ("a", "b")
    ]
    async with create_deep_agent(
        model(root), subagents=children, openeight=config()
    ) as agent:
        await agent.ainvoke({"messages": [("user", "go")]})
        await agent.aflush()
        client = PolicyClient.instances[0]
        assert sorted(effects) == ["a", "b"]
        assert (
            len(client.requests) == 4
            and len({r["run_id"] for r in client.requests}) == 1
        )
        runs = [r for batch in client.writes for r in batch.get("runs", [])]
        assert [r["status"] for r in runs] == ["active", "completed"]


async def test_application_retry_authorizes_each_final_argument_set():
    class Retry(AgentMiddleware):
        async def awrap_tool_call(self, request, handler):
            final = request.override(
                tool_call={**request.tool_call, "args": {"value": "edited"}}
            )
            await handler(final)
            return await handler(final)

    effects = []
    async with create_deep_agent(
        model(call()),
        tools=[effect(effects)],
        middleware=[Retry()],
        openeight=config(),
    ) as agent:
        await agent.ainvoke({"messages": [("user", "go")]})
        requests = PolicyClient.instances[0].requests
        assert effects == ["edited", "edited"] and len(requests) == 2
        assert requests[0]["decision_id"] != requests[1]["decision_id"]
        assert all(
            r["selected_values"] == [{"path": "value", "value": "edited"}]
            for r in requests
        )


@pytest.mark.parametrize("mode", ["values", "updates", "messages"])
async def test_stream_modes_execute_once(mode):
    effects = []
    async with create_deep_agent(
        model(call()), tools=[effect(effects)], openeight=config()
    ) as agent:
        chunks = [
            chunk
            async for chunk in agent.astream(
                {"messages": [("user", "go")]}, stream_mode=mode
            )
        ]
        assert chunks and effects == ["original"]
        assert len(PolicyClient.instances[0].requests) == 1


async def test_cancellation_retains_effect_and_context_exit_until_settlement():
    started, release = asyncio.Event(), asyncio.Event()
    effects = []

    @tool
    async def wait(value: str) -> str:
        """Wait before recording the effect."""
        started.set()
        await release.wait()
        effects.append(value)
        return value

    context = create_deep_agent(model(call("wait")), tools=[wait], openeight=config())
    agent = await context.__aenter__()
    pending = asyncio.create_task(agent.ainvoke({"messages": [("user", "go")]}))
    await asyncio.wait_for(started.wait(), 2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    with pytest.raises(CleanupIncompleteError):
        await agent.aclose(0.01)
    status = agent.delivery_status()
    assert not status.closed and not PolicyClient.instances[0].closed
    closing = asyncio.create_task(context.__aexit__(None, None, None))
    await asyncio.sleep(0.02)
    closing.cancel()
    await asyncio.sleep(0.02)
    closing.cancel()
    assert not closing.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, 2)
    assert effects == ["original"] and PolicyClient.instances[0].closed


async def test_scope_failure_precedes_checkpoint_read_and_model(tmp_path, monkeypatch):
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "state.db")) as saver:
        reads = []
        original = saver.aget_tuple

        async def read(cfg):
            reads.append(cfg)
            return await original(cfg)

        monkeypatch.setattr(saver, "aget_tuple", read)
        async with create_deep_agent(
            model(), openeight=config(), checkpointer=saver
        ) as agent:
            thread = agent.new_thread()

            def reject(ids):
                raise ConfigurationError("client_closed")

            monkeypatch.setattr(PolicyClient.instances[0], "get_tool_readiness", reject)
            with pytest.raises(ConfigurationError):
                await agent.ainvoke(
                    {"messages": [("user", "secret")]},
                    {"configurable": {"thread_id": thread}},
                )
            assert not reads


async def test_diagnostics_and_thread_overrides_reject_before_execution():
    async with create_deep_agent(model(), openeight=config()) as agent:
        with pytest.raises(ConfigurationError):
            await agent.ainvoke(
                {"messages": []}, {"configurable": {"checkpoint_id": "foreign"}}
            )
        with pytest.raises(ConfigurationError):
            await anext(agent.astream({"messages": []}, stream_mode="debug"))
        assert not PolicyClient.instances[0].requests


async def test_model_only_batch_owns_distinct_runs():
    async with create_deep_agent(model(), openeight=config()) as agent:
        result = await agent.abatch(
            [{"messages": [("user", "a")]}, {"messages": [("user", "b")]}]
        )
        await agent.aflush()
        assert len(result) == 2
        runs = [
            r
            for batch in PolicyClient.instances[0].writes
            for r in batch.get("runs", [])
        ]
        assert len({r["id"] for r in runs}) == 2
        assert sum(r["status"] == "completed" for r in runs) == 2


@pytest.mark.parametrize("subgraphs", [True, False])
async def test_message_stream_interrupt_can_resume(tmp_path, subgraphs):
    effects = []
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "state.db")) as saver:
        async with create_deep_agent(
            model(call()),
            tools=[effect(effects)],
            openeight=config(),
            checkpointer=saver,
            interrupt_on={"effect": True},
        ) as agent:
            cfg = {"configurable": {"thread_id": agent.new_thread()}}
            chunks = [
                chunk
                async for chunk in agent.astream(
                    {"messages": [("user", "go")]},
                    cfg,
                    stream_mode="messages",
                    subgraphs=subgraphs,
                )
            ]
            assert chunks and not effects
            await agent.ainvoke(
                Command(resume={"decisions": [{"type": "approve"}]}), cfg
            )
            assert effects == ["original"]


async def test_duplicate_native_tools_reject_before_construction():
    with pytest.raises(ConfigurationError):
        create_deep_agent(model(), tools=[effect([]), effect([])], openeight=config())
    assert not PolicyClient.instances


async def test_foreign_event_loop_rejects_execution_and_close():
    effects = []
    async with create_deep_agent(
        model(call()), tools=[effect(effects)], openeight=config()
    ) as agent:

        async def other():
            with pytest.raises(ConfigurationError) as error:
                await agent.ainvoke({"messages": [("user", "go")]})
            assert error.value.reason == "integration_loop_changed"
            with pytest.raises(ConfigurationError):
                await agent.aclose()

        await asyncio.to_thread(lambda: asyncio.run(other()))
        assert not effects and not PolicyClient.instances[0].closed


async def test_checkpoint_admission_retains_transport_during_close(
    tmp_path, monkeypatch
):
    import threading

    started, release = threading.Event(), threading.Event()
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "state.db")) as saver:
        async with create_deep_agent(
            model(), openeight=config(), checkpointer=saver
        ) as agent:

            def readiness(ids):
                started.set()
                release.wait(3)
                return ()

            client = PolicyClient.instances[0]
            monkeypatch.setattr(client, "get_tool_readiness", readiness)
            thread = agent.new_thread()
            invocation = asyncio.create_task(
                agent.ainvoke(
                    {"messages": [("user", "go")]},
                    {"configurable": {"thread_id": thread}},
                )
            )
            await asyncio.to_thread(started.wait, 2)
            try:
                with pytest.raises(CleanupIncompleteError):
                    await agent.aclose(0.01)
                status = agent.delivery_status()
                assert not status.closed and not client.closed
            finally:
                release.set()
            with pytest.raises(ConfigurationError):
                await invocation
            assert not client.writes


async def test_readiness_call_retains_transport_during_close(monkeypatch):
    import threading

    started, release = threading.Event(), threading.Event()
    async with create_deep_agent(model(), openeight=config()) as agent:
        client = PolicyClient.instances[0]

        def readiness(ids):
            started.set()
            release.wait(3)
            return ()

        monkeypatch.setattr(client, "get_tool_readiness", readiness)
        pending = asyncio.create_task(agent.areadiness())
        await asyncio.to_thread(started.wait, 2)
        try:
            with pytest.raises(CleanupIncompleteError):
                await agent.aclose(0.01)
            status = agent.delivery_status()
            assert not status.closed and not client.closed
        finally:
            release.set()
        await pending


async def test_generated_tool_collision_rejects_before_entry():
    candidate = effect([])
    context = create_deep_agent(model(), tools=[candidate], openeight=config())
    candidate.name = "read_file"
    with pytest.raises(ConfigurationError):
        await context.__aenter__()
    assert not PolicyClient.instances


@pytest.mark.parametrize("adapter", ["langchain", "deepagents"])
async def test_release_thread_reclaims_capacity_without_deleting_saved_state(
    tmp_path, adapter
):
    from openeight.langchain import create_agent

    build = create_agent if adapter == "langchain" else create_deep_agent
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "threads.db")) as saver:
        async with build(model(), openeight=config(), checkpointer=saver) as agent:
            threads = [agent.new_thread() for _ in range(1000)]
            with pytest.raises(ConfigurationError) as error:
                agent.new_thread()
            assert error.value.reason == "execution_busy"
            cfg = {"configurable": {"thread_id": threads[0]}}
            await agent.ainvoke({"messages": [("user", "saved")]}, cfg)
            physical = {
                "configurable": {"thread_id": agent._session._saver.prefix + threads[0]}
            }
            assert await saver.aget_tuple(physical) is not None
            agent.release_thread(threads[0])
            replacement = agent.new_thread()
            assert replacement not in threads
            assert await saver.aget_tuple(physical) is not None
            with pytest.raises(ConfigurationError) as error:
                await agent.ainvoke({"messages": [("user", "unowned")]}, cfg)
            assert error.value.reason == "checkpoint_thread_unowned"
            with pytest.raises(ConfigurationError) as error:
                agent.release_thread(threads[0])
            assert error.value.reason == "checkpoint_thread_unowned"
            result = await agent.ainvoke(
                {"messages": [("user", "still owned")]},
                {"configurable": {"thread_id": threads[1]}},
            )
            assert result["messages"][-1].content == "done"
        with pytest.raises(ConfigurationError):
            agent.release_thread(replacement)


async def test_release_thread_refuses_active_and_cancelled_but_unsettled_attempt(
    tmp_path,
):
    started, finish = asyncio.Event(), asyncio.Event()

    @tool
    async def hold(value: str) -> str:
        """Wait until the admitted handler settles."""
        started.set()
        await finish.wait()
        return value

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "busy.db")) as saver:
        async with create_deep_agent(
            model(call("hold")), tools=[hold], openeight=config(), checkpointer=saver
        ) as agent:
            thread = agent.new_thread()
            pending = asyncio.create_task(
                agent.ainvoke(
                    {"messages": [("user", "go")]},
                    {"configurable": {"thread_id": thread}},
                )
            )
            try:
                await asyncio.wait_for(started.wait(), 2)
                for cancel in [False, True]:
                    if cancel:
                        pending.cancel()
                        await asyncio.sleep(0)
                    with pytest.raises(ConfigurationError) as error:
                        agent.release_thread(thread)
                    assert error.value.reason == "checkpoint_thread_busy"
            finally:
                finish.set()
                await asyncio.gather(pending, return_exceptions=True)
            agent.release_thread(thread)
            assert thread not in agent._session._saver.threads


@pytest.mark.parametrize("mode", ["none", "inputs", "outputs", "all"])
async def test_child_content_policy_is_shared_without_affecting_native_results(mode):
    import json

    calls, effects = [], []

    def redact(ctx, value):
        calls.append((ctx.kind, ctx.direction))
        return "[REDACTED]"

    async with create_deep_agent(
        model(call("task", description="work", subagent_type="researcher")),
        subagents=[
            dict(
                name="researcher",
                description="work",
                model=model(call(value="secret-marker")),
                tools=[effect(effects)],
            )
        ],
        openeight=config(capture_content=mode, redact_content=redact),
    ) as agent:
        result = await agent.ainvoke({"messages": [("user", "secret-marker")]})
        assert result["messages"][-1].content == "done" and effects == ["secret-marker"]
        await agent.aflush()
        events = [
            e
            for body in PolicyClient.instances[0].writes
            for e in body.get("events", [])
        ]
        models = [e for e in events if e["observation_kind"] == "llm_call"]
        assert len(models) >= 4
        assert len({e["run_id"] for e in models}) == 1
        for event in models:
            assert ("input_json" in event) == (mode in ("inputs", "all"))
            assert ("output_json" in event) == (mode in ("outputs", "all"))
            assert "secret-marker" not in json.dumps(event)
    assert bool(calls) == (mode != "none")


async def test_deepagents_observation_declares_its_adapter():
    async with observe(
        ToolModel(responses=[AIMessage(content="done")]),
        openeight={
            "agent_name": "deep-observed",
            "endpoint": "http://localhost:7777",
            "credential": "ctk_secret",
        },
    ):
        assert PolicyClient.instances[-1].integration == "deepagents"
