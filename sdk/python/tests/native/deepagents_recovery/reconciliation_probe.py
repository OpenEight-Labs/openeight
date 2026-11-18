"""Prove a native control-flow pause leaves uncertain business output uncommitted."""

import asyncio
import json
import sys
from pathlib import Path

from deepagents import create_deep_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphInterrupt
from probe import calls, model


async def run(directory, phase):
    receipt = directory / "receipt"
    effects = directory / "effects"

    @tool
    async def effect(value: str) -> str:
        """Retain the synthetic destination result."""
        count = int(effects.read_text()) if effects.exists() else 0
        effects.write_text(str(count + 1))
        receipt.write_text(value)
        return value

    class Reconcile(AgentMiddleware):
        async def awrap_tool_call(self, request, handler):
            if receipt.exists():
                return ToolMessage(
                    receipt.read_text(),
                    name="effect",
                    tool_call_id=request.tool_call["id"],
                )
            await handler(request)
            raise GraphInterrupt(())

    child = dict(
        name="worker",
        description="work",
        model=model(calls(("same", "effect", {"value": "once"}))),
        tools=[effect],
        middleware=[Reconcile()],
    )
    async with AsyncSqliteSaver.from_conn_string(
        str(directory / "checkpoints.sqlite")
    ) as saver:
        saver.serde = JsonPlusSerializer(pickle_fallback=False)
        graph = create_deep_agent(
            model(
                calls(
                    ("same", "task", {"description": "work", "subagent_type": "worker"})
                )
            ),
            tools=[],
            subagents=[child],
            checkpointer=saver,
        )
        config = {
            "configurable": {"thread_id": "reconciliation"},
            "recursion_limit": 100,
        }
        await graph.ainvoke(
            {"messages": [("user", "go")]} if phase == "start" else None,
            config,
            durability="sync",
        )
        snapshot = await graph.aget_state(config)
        (directory / (phase + ".json")).write_text(
            json.dumps(
                {
                    "next": list(snapshot.next),
                    "errors": [str(t.error) for t in snapshot.tasks if t.error],
                    "effects": int(effects.read_text()),
                    "messages": [
                        m.content
                        for m in snapshot.values["messages"]
                        if m.type == "ai" and not m.tool_calls
                    ],
                }
            )
        )


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]), sys.argv[2]))
