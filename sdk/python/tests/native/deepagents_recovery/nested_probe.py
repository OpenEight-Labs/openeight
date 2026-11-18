"""Native root/child/grandchild restart feasibility without SDK composition."""

import asyncio
import json
import sys
from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from deepagents.middleware.subagents import SubAgentMiddleware
from langchain_core.tools import tool
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from probe import Gate, append, calls, model


async def run(directory, phase):
    @tool
    async def effect(value: str) -> str:
        """Record the deepest child's native destination effect."""
        append(directory / "effects.jsonl", {"value": value})
        return value

    backend = StateBackend()
    leaf = dict(
        name="leaf",
        description="leaf work",
        system_prompt="leaf work",
        model=model(calls(("same-call", "effect", {"value": "once"}))),
        tools=[effect],
        middleware=[Gate(directory, "worker/leaf")],
    )
    nested = SubAgentMiddleware(backend=backend, subagents=[leaf])
    worker = dict(
        name="worker",
        description="delegated work",
        system_prompt="delegate to leaf",
        model=model(
            calls(
                (
                    "same-call",
                    "task",
                    {"description": "leaf work", "subagent_type": "leaf"},
                )
            )
        ),
        tools=[],
        middleware=[nested, Gate(directory, "worker")],
    )
    async with AsyncSqliteSaver.from_conn_string(
        str(directory / "checkpoints.sqlite")
    ) as saver:
        saver.serde = JsonPlusSerializer(pickle_fallback=False)
        graph = create_deep_agent(
            model(
                calls(
                    (
                        "same-call",
                        "task",
                        {"description": "delegated work", "subagent_type": "worker"},
                    )
                )
            ),
            tools=[],
            subagents=[worker],
            backend=backend,
            middleware=[Gate(directory, "root")],
            checkpointer=saver,
        )
        config = {"configurable": {"thread_id": "nested"}, "recursion_limit": 100}
        before = await graph.aget_state(config)
        if phase == "start":
            value = {"messages": [("user", "go")]}
        else:
            interrupts = [i for task in before.tasks for i in task.interrupts]
            assert len(interrupts) == 1
            value = Command(
                resume={i.id: {"decision_id": "child-approval"} for i in interrupts}
            )
        await graph.ainvoke(value, config, durability="sync")
        after = await graph.aget_state(config)
        (directory / (phase + ".json")).write_text(
            json.dumps(
                {
                    "next": list(after.next),
                    "interrupts": [
                        i.id for task in after.tasks for i in task.interrupts
                    ],
                    "output": [
                        m.content
                        for m in after.values.get("messages", ())
                        if m.type == "ai" and not m.tool_calls
                    ],
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]), sys.argv[2]))
