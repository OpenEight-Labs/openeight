"""Isolated native restart probe; does not enable SDK durable execution."""

import asyncio
import json
import sys
from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from langchain.agents.middleware import AgentMiddleware, TodoListMiddleware
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.config import get_config
from langgraph.types import Command, interrupt


def append(path, value):
    with path.open("a") as file:
        file.write(json.dumps(value, sort_keys=True) + "\n")


class ScriptModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, *args, **kwargs):
        # Replay is determined by checkpointed conversation, not process-local counters.
        index = sum(isinstance(m, AIMessage) for m in messages)
        self.i = min(index, len(self.responses) - 1)
        return super()._generate(messages, *args, **kwargs)


def calls(*items):
    return AIMessage(
        content="", tool_calls=[dict(id=i, name=n, args=a) for i, n, a in items]
    )


def model(*items):
    return ScriptModel(responses=[*items, AIMessage(content="done")])


class Gate(AgentMiddleware):
    def __init__(self, directory, label):
        self.directory, self.label = directory, label

    async def awrap_tool_call(self, request, handler):
        config = get_config()
        selectors = config.get("configurable", {})
        append(
            self.directory / "calls.jsonl",
            {
                "label": self.label,
                "id": request.tool_call["id"],
                "name": request.tool_call["name"],
                "namespace": selectors.get("checkpoint_ns"),
                "metadata": config.get("metadata", {}),
                "runtime_config_keys": sorted(selectors),
                "checkpoint_map": selectors.get("checkpoint_map"),
                "task_id": selectors.get("__pregel_task_id"),
            },
        )
        if request.tool_call["name"] == "effect":
            result = interrupt({"decision_id": "child-approval"})
            assert result == {"decision_id": "child-approval"}
        return await handler(request)


async def run(directory, phase):
    @tool
    async def effect(value: str) -> str:
        """Record a destination effect for the probe."""
        append(directory / "effects.jsonl", {"value": value})
        return value

    child = dict(
        name="worker",
        description="work",
        system_prompt="work",
        model=model(
            calls(
                (
                    "child-file",
                    "write_file",
                    {"file_path": "/child.txt", "content": "child artifact"},
                )
            ),
            calls(("business", "effect", {"value": "once"})),
        ),
        tools=[effect],
        middleware=[Gate(directory, "worker")],
    )
    sibling = dict(
        name="sibling",
        description="sibling",
        system_prompt="sibling",
        model=model(
            calls(
                (
                    "sibling-file",
                    "write_file",
                    {"file_path": "/sibling.txt", "content": "sibling artifact"},
                )
            )
        ),
        tools=[],
        middleware=[Gate(directory, "sibling")],
    )
    async with AsyncSqliteSaver.from_conn_string(
        str(directory / "checkpoints.sqlite")
    ) as saver:
        graph = create_deep_agent(
            model(
                calls(
                    (
                        "todos",
                        "write_todos",
                        {
                            "todos": [
                                {"content": "investigate", "status": "in_progress"}
                            ]
                        },
                    )
                ),
                calls(
                    (
                        "root-file",
                        "write_file",
                        {"file_path": "/root.txt", "content": "root artifact"},
                    )
                ),
                calls(
                    (
                        "delegate-sibling",
                        "task",
                        {"description": "sibling", "subagent_type": "sibling"},
                    )
                ),
                calls(
                    (
                        "delegate-worker",
                        "task",
                        {"description": "worker", "subagent_type": "worker"},
                    )
                ),
            ),
            tools=[],
            subagents=[child, sibling],
            middleware=[TodoListMiddleware(), Gate(directory, "root")],
            backend=StateBackend(),
            checkpointer=saver,
        )
        config = {"configurable": {"thread_id": "native-restart-probe"}}
        if phase == "start":
            result = await graph.ainvoke({"messages": [("user", "go")]}, config)
            assert result.get("__interrupt__"), result
            assert not (directory / "effects.jsonl").exists()
        elif phase == "resume":
            result = await graph.ainvoke(
                Command(resume={"decision_id": "child-approval"}), config
            )
            assert not result.get("__interrupt__")
        else:
            raise ValueError(phase)
        state = await graph.aget_state(config, subgraphs=True)

        def summarize(snapshot):
            return dict(
                config=snapshot.config,
                next=list(snapshot.next),
                interrupts=[dict(id=i.id, value=i.value) for i in snapshot.interrupts],
                files=snapshot.values.get("files", {}),
                todos=snapshot.values.get("todos", []),
                tool_messages=[
                    dict(name=m.name, content=m.content, status=m.status)
                    for m in snapshot.values.get("messages", [])
                    if m.type == "tool"
                ],
                tasks=[
                    dict(
                        id=t.id,
                        name=t.name,
                        path=list(t.path),
                        state=summarize(t.state)
                        if hasattr(t.state, "values")
                        else t.state,
                    )
                    for t in snapshot.tasks
                ],
            )

        (directory / f"{phase}.json").write_text(
            json.dumps(summarize(state), sort_keys=True, indent=2, default=str)
        )


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]), sys.argv[2]))
