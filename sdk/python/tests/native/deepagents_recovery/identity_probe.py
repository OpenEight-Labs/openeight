"""Native same-child collision/parent isolation probe; no SDK execution enabled."""

import asyncio
import json
import sys
from contextvars import ContextVar
from inspect import getclosurevars
from pathlib import Path
from types import SimpleNamespace

from deepagents import create_deep_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.config import get_config
from langgraph.types import Command, interrupt
from probe import append, calls, model

from openeight import ConfigurationError
from openeight.deepagents._continuation import DurableCallContext, durable_attempt_key
from openeight.deepagents._frameworks._checkpoints import read_deep_checkpoint
from openeight.deepagents._frameworks._identity import NativeObservations
from openeight.deepagents._parents import (
    CURRENT_PARENT_DELEGATION,
    parent_delegation,
    validate_parent_delegation,
)
from openeight.deepagents.durable import DurableContinuation

PARENT = ContextVar("native-probe-parent", default=None)


class IdentityGate(AgentMiddleware):
    def __init__(self, directory, observations, path, phase, invocation):
        self.invocation = invocation
        self.directory, self.observations, self.path, self.phase = (
            directory,
            observations,
            path,
            phase,
        )

    async def awrap_tool_call(self, request, handler):
        identity = self.observations.observe(
            get_config(), request.tool_call, child_path=self.path
        )
        append(
            self.directory / "identities.jsonl",
            {
                "phase": self.phase,
                "path": self.path,
                "call_id": request.tool_call["id"],
                "namespace": identity.checkpoint_namespace,
                "ancestors": identity.ancestor_namespaces,
                "parent_id": identity.parent_native_tool_call_id,
            },
        )
        if request.tool_call["name"] == "task":
            key = durable_attempt_key(
                DurableCallContext(
                    self.path, identity.checkpoint_namespace, None, "delegation"
                ),
                request.tool_call["id"],
            )
            continuation = DurableContinuation(
                "native-probe",
                "same-child",
                request.tool_call["id"],
                "[]",
                identity.checkpoint_namespace,
                request.tool_call["id"],
                key,
                "delegation",
            )
            attempt = SimpleNamespace(
                deployment_id="native-probe",
                thread_id="same-child",
                decision_id=request.tool_call["id"],
                tool_call_id=key,
                status="executing",
                native_call_json=json.dumps(request.tool_call),
            )
            self.invocation.records[key] = (attempt, continuation)
            if self.phase in {"start", "swap"}:
                while len(self.invocation.records) < 2:
                    await asyncio.sleep(0.001)
            token = PARENT.set(request.tool_call["id"])
            authority = CURRENT_PARENT_DELEGATION.set(
                parent_delegation(attempt, continuation, target_child_path=("worker",))
            )
            try:
                return await handler(request)
            finally:
                CURRENT_PARENT_DELEGATION.reset(authority)
                PARENT.reset(token)
        if self.phase == "swap" and identity.parent_native_tool_call_id == "parent-A":
            other = next(
                (a, c)
                for a, c in self.invocation.records.values()
                if c.native_tool_call_id == "parent-B"
            )
            token = CURRENT_PARENT_DELEGATION.set(
                parent_delegation(*other, target_child_path=("worker",))
            )
            try:
                validate_parent_delegation(
                    CURRENT_PARENT_DELEGATION.get(),
                    invocation=self.invocation,
                    native=identity,
                )
            except ConfigurationError as error:
                assert error.reason == "invalid_resume_correlation"
                append(self.directory / "rejected.jsonl", {"parent": "parent-A"})
                raise
            else:
                raise AssertionError("swapped authority accepted")
            finally:
                CURRENT_PARENT_DELEGATION.reset(token)
        validate_parent_delegation(
            CURRENT_PARENT_DELEGATION.get(), invocation=self.invocation, native=identity
        )
        append(
            self.directory / "prepared.jsonl",
            {"phase": self.phase, "parent": identity.parent_native_tool_call_id},
        )
        value = interrupt({"decision_id": identity.parent_native_tool_call_id})
        assert value == {"decision_id": identity.parent_native_tool_call_id}
        return await handler(request)


async def run(directory, phase):
    observations = NativeObservations(paths=[(), ("worker",)], thread_id="same-child")

    records = {}
    invocation = SimpleNamespace(
        deployment_id="native-probe",
        thread_id="same-child",
        records=records,
        native_observations=observations,
        owner=SimpleNamespace(
            _composition={
                "topology": [
                    {"path": [], "delegates_to": [["worker"]]},
                    {"path": ["worker"], "delegates_to": []},
                ]
            }
        ),
        store=SimpleNamespace(
            continuation=lambda thread, key: records.get(key, (None, None))[1],
            get_attempt=lambda thread, key: records.get(key, (None, None))[0],
        ),
    )

    @tool
    async def effect() -> str:
        """Record one native effect under its exact parent."""
        append(directory / "effects.jsonl", {"parent": PARENT.get()})
        return "done"

    child = dict(
        name="worker",
        description="work",
        tools=[effect],
        system_prompt="work",
        model=model(calls(("same-child-call", "effect", {}))),
        middleware=[
            IdentityGate(directory, observations, ("worker",), phase, invocation)
        ],
    )
    root = model(
        calls(
            ("parent-A", "task", {"description": "A", "subagent_type": "worker"}),
            ("parent-B", "task", {"description": "B", "subagent_type": "worker"}),
        )
    )
    async with AsyncSqliteSaver.from_conn_string(
        str(directory / "checkpoints.sqlite")
    ) as saver:
        graph = create_deep_agent(
            root,
            tools=[],
            subagents=[child],
            checkpointer=saver,
            middleware=[IdentityGate(directory, observations, (), phase, invocation)],
        )
        cfg = {"configurable": {"thread_id": "same-child"}}

        async def preflight():
            native_task = graph.nodes["tools"].bound.tools_by_name["task"]
            children = getclosurevars(native_task.coroutine).nonlocals[
                "subagent_graphs"
            ]
            graphs = {(): graph, **{(name,): child for name, child in children.items()}}
            compatibility = SimpleNamespace(
                _graphs=graphs,
                _bindings=SimpleNamespace(
                    resolve=lambda path, name: graphs[path]
                    .nodes["tools"]
                    .bound.tools_by_name.get(name)
                ),
            )
            projection = await read_deep_checkpoint(compatibility, graph, cfg)
            if projection.has_next:
                assert projection.native.observations.observations
            return projection

        if phase != "start":
            saved = await preflight()
            assert saved.has_next
            assert (
                {
                    o.native_call_id
                    for o in saved.native.observations.observations.values()
                    if not o.identity.child_path
                }
                == {"parent-A", "parent-B"}
                if phase in {"resume-A", "swap"}
                else True
            )

        if phase == "start":
            result = await graph.ainvoke(
                {"messages": [HumanMessage(content="go", id="native-command")]}, cfg
            )
        else:
            pauses = json.loads((directory / "interrupts.json").read_text())
            parent = "parent-A" if phase in {"resume-A", "swap"} else "parent-B"
            identifier = next(
                i["id"] for i in pauses if i["value"]["decision_id"] == parent
            )
            try:
                result = await graph.ainvoke(
                    Command(resume={identifier: {"decision_id": parent}}), cfg
                )
            except ConfigurationError as error:
                if phase != "swap" or error.reason != "invalid_resume_correlation":
                    raise
                assert not (directory / "effects.jsonl").exists()
                projection = await preflight()
                assert any(
                    i.value.get("decision_id") == "parent-B"
                    for task in projection.native.tasks
                    for i in task.task.interrupts
                )
                return
        await preflight()
        interrupts = [
            {"id": i.id, "value": i.value} for i in result.get("__interrupt__", [])
        ]
        (directory / "interrupts.json").write_text(json.dumps(interrupts))
        if phase == "start":
            assert len(interrupts) == 2
        elif phase == "resume-A":
            assert (
                len(interrupts) == 1
                and interrupts[0]["value"]["decision_id"] == "parent-B"
            )
        elif phase == "resume-B":
            assert interrupts == []


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]), sys.argv[2]))
