"""Governed static root/worker/leaf execution keeps each native ancestry."""

from typing import Annotated

import pytest

pytest.importorskip("deepagents")
pytest.importorskip("aiosqlite")

from deepagents.backends import StateBackend
from deepagents.middleware.subagents import SubAgentMiddleware
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.messages import AIMessage
from langchain_core.tools import InjectedToolArg, tool
from test_deep_durable_engine import HistoryModel, NativeClient, candidate_selection
from test_local_durable_facade import INPUT, configure_catalog
from test_managed_langchain import config

from openeight.deepagents import LocalDurability, ToolSurface, create_deep_agent
from openeight.langchain import _owner


def call(name, args):
    return AIMessage(
        content="", tool_calls=[{"id": "same-call", "name": name, "args": args}]
    )


def nested_agent(directory, effects, *, siblings=False):
    @tool
    async def effect(
        value: str, idempotency_key: Annotated[str, InjectedToolArg]
    ) -> str:
        """Record the deepest governed effect."""
        effects.append(value)
        return value

    leaf = dict(
        name="leaf",
        description="deepest work",
        system_prompt="leaf work",
        model=HistoryModel(
            responses=[
                call("effect", {"value": "once"}),
                AIMessage(content="leaf done"),
            ]
        ),
        tools=[effect],
        middleware=[TodoListMiddleware()],
    )
    nested = SubAgentMiddleware(
        backend=StateBackend(),
        subagents=[leaf, {**leaf, "name": "other"}] if siblings else [leaf],
    )
    worker = dict(
        name="worker",
        description="worker",
        system_prompt="delegate",
        model=HistoryModel(
            responses=[
                call("task", {"description": "leaf work", "subagent_type": "leaf"}),
                AIMessage(content="worker done"),
            ]
        ),
        tools=[],
        middleware=[nested],
    )
    if siblings:
        worker["model"].responses[0] = AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "delegate-a",
                    "name": "task",
                    "args": {"description": "first", "subagent_type": "leaf"},
                },
                {
                    "id": "delegate-b",
                    "name": "task",
                    "args": {"description": "second", "subagent_type": "other"},
                },
            ],
        )
    root = HistoryModel(
        responses=[
            call("task", {"description": "worker work", "subagent_type": "worker"}),
            AIMessage(content="root done"),
        ]
    )
    return create_deep_agent(
        root,
        openeight=config(),
        durability=LocalDurability(directory=directory, revision="nested-1"),
        subagents=[worker],
        include_general_purpose=False,
        tool_surfaces=[
            ToolSurface((), ("read_file", "task")),
            ToolSurface(("worker",), ("read_file", "task")),
            ToolSurface(("worker", "leaf"), ("effect", "write_todos")),
            *(
                [ToolSurface(("worker", "other"), ("effect", "write_todos"))]
                if siblings
                else []
            ),
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("approval", ["approved", "rejected"])
async def test_nested_approval_reopen_and_duplicate_native_ids_keep_parent_chain(
    tmp_path, monkeypatch, approval
):
    candidate_selection(monkeypatch)
    client = NativeClient()
    client.state = "pending"
    monkeypatch.setattr(_owner, "Client", lambda *a, **kw: client)
    effects = []
    async with nested_agent(tmp_path, effects) as agent:
        configure_catalog(client, agent)
        client.approval_names = {client.binding_names[("worker", "leaf"), "effect"]}
        await agent.ainitialize()
        paused = await agent.astart(INPUT, command_id="start")
        assert paused.status == "waiting" and len(paused.pending_decision_ids) == 1
        before = agent._local_runtime.store.attempts(paused.thread_id)
        assert len(before) == 3 and effects == []
        assert len({a.tool_call_id for a in before}) == 3
        assert len({a.decision_id for a in before}) == 3
        root_run = paused.run_id
        publications = len(client.registrations)
    client.state = approval
    async with nested_agent(tmp_path, effects) as agent:
        configure_catalog(client, agent)
        result = await agent.aresume(thread_id=paused.thread_id, command_id="resume")
        assert result.status == "completed" and result.output == ("root done",)
        assert result.run_id == root_run
        assert effects == (["once"] if approval == "approved" else [])
        assert len(client.registrations) == publications
        after = agent._local_runtime.store.attempts(paused.thread_id)
        assert len(after) == 3 and {a.decision_id for a in after} == {
            a.decision_id for a in before
        }
        assert (
            await agent.aresume(thread_id=paused.thread_id, command_id="resume")
            == result
        )


@pytest.mark.asyncio
async def test_nested_sibling_approval_preserves_completed_leaf_across_second_reopen(
    tmp_path, monkeypatch
):
    candidate_selection(monkeypatch)
    client = NativeClient()
    client.state = "pending"
    monkeypatch.setattr(_owner, "Client", lambda *a, **kw: client)
    effects = []
    async with nested_agent(tmp_path, effects, siblings=True) as agent:
        configure_catalog(client, agent)
        client.approval_names = {
            client.binding_names[("worker", name), "effect"]
            for name in ("leaf", "other")
        }
        await agent.ainitialize()
        waiting = await agent.astart(INPUT, command_id="start")
        assert waiting.status == "waiting" and len(waiting.pending_decision_ids) == 2
    first, second = waiting.pending_decision_ids
    client.approval_states = {first: "approved", second: "pending"}
    async with nested_agent(tmp_path, effects, siblings=True) as agent:
        configure_catalog(client, agent)
        partial = await agent.aresume(thread_id=waiting.thread_id, command_id="first")
        assert partial.status == "waiting" and partial.pending_decision_ids == (second,)
        assert effects == ["once"]
    client.approval_states[second] = "approved"
    async with nested_agent(tmp_path, effects, siblings=True) as agent:
        configure_catalog(client, agent)
        complete = await agent.aresume(thread_id=waiting.thread_id, command_id="second")
        assert complete.status == "completed" and complete.run_id == waiting.run_id
        assert effects == ["once", "once"]
        attempts = agent._local_runtime.store.attempts(waiting.thread_id)
        assert len(attempts) == 5 and all(a.status == "succeeded" for a in attempts)
