"""Process-boundary qualification using the public adapter and a real native saver."""

import asyncio
import importlib.util
import sys
from pathlib import Path

from _cookbook import recipe
from _external_control import ExternalControlPlane
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from test_framework_boundary import ToolModel

from openeight import _adapter_http
from openeight.checkpoints import CheckpointBinding
from openeight.langchain import create_agent


async def main():
    directory, mode = Path(sys.argv[1]), sys.argv[2]
    spec = importlib.util.spec_from_file_location(
        "application",
        recipe("patterns/checkpoint-recovery") / "application.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    authenticated = object()

    async def authorize(*, config, context):
        assert context is authenticated
        assert config == {"configurable": {"thread_id": "application-thread"}}
        return CheckpointBinding(
            scope_id="scope-a",
            thread_id="application-thread",
            checkpoint_ns="",
            graph_revision="release-1",
            model_variant=None,
            mode=mode,
        )

    access = module.ApplicationCheckpoints(
        directory / "sidecar.sqlite", authorize=authorize
    )
    control = ExternalControlPlane()
    _adapter_http.DeadlineHTTPTransport = lambda: control
    async with AsyncSqliteSaver.from_conn_string(
        str(directory / "native.sqlite")
    ) as saver:
        async with create_agent(
            ToolModel(responses=[AIMessage(content="done-" + mode)]),
            checkpointer=saver,
            openeight={
                "agent_name": "support",
                "endpoint": "https://control.example",
                "credential": "ctk_test",
                "checkpoints": access,
            },
        ) as agent:
            result = await agent.ainvoke(
                {"messages": [("user", mode)]},
                {"configurable": {"thread_id": "application-thread"}},
                context=authenticated,
            )
            contents = [message.content for message in result["messages"]]
            assert contents == (
                ["new", "done-new"]
                if mode == "new"
                else ["new", "done-new", "continue", "done-continue"]
            )
    print("qualified " + mode)


asyncio.run(main())
