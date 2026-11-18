"""Counterexample: content/status settlement cannot restore queued file updates."""

import asyncio
import json
import os
import sys
from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from probe import calls, model

CAPTURED = []


class SettlementGap(AgentMiddleware):
    def __init__(self, directory, phase, backend):
        self.directory, self.phase, self.backend = directory, phase, backend

    async def awrap_tool_call(self, request, handler):
        assert request.tool_call["name"] == "write_file"
        if self.phase in {"replay", "restore"}:
            if self.phase == "restore":
                (update,) = json.loads(
                    (self.directory / "queued-writes.json").read_text()
                )
                self.backend._send_files_update(update)
            saved = json.loads((self.directory / "settlement.json").read_text())
            return ToolMessage(
                **saved,
                tool_call_id=request.tool_call["id"],
                name=request.tool_call["name"],
            )
        result = await handler(request)
        assert isinstance(result, ToolMessage)
        # This is exactly the existing result_json information, not a native receipt.
        (self.directory / "settlement.json").write_text(
            json.dumps({"content": result.content, "status": result.status})
        )
        (self.directory / "queued-writes.json").write_text(
            json.dumps(CAPTURED, default=str)
        )
        # A process kill here prevents the native node's checkpoint from completing.
        os._exit(73)


async def run(directory, phase):
    backend = StateBackend()
    send = backend._send_files_update

    def capture(update):
        CAPTURED.append(update)
        return send(update)

    backend._send_files_update = capture
    async with AsyncSqliteSaver.from_conn_string(
        str(directory / "checkpoints.sqlite")
    ) as saver:
        graph = create_deep_agent(
            model(
                calls(
                    (
                        "write",
                        "write_file",
                        {"file_path": "/artifact.txt", "content": "must survive"},
                    )
                )
            ),
            tools=[],
            backend=backend,
            checkpointer=saver,
            middleware=[SettlementGap(directory, phase, backend)],
        )
        cfg = {"configurable": {"thread_id": "crash-gap"}}
        # Persist the preceding step before killing inside the tool node.
        await graph.ainvoke(
            {"messages": [("user", "go")]} if phase == "kill" else None,
            cfg,
            durability="sync",
        )
        snapshot = await graph.aget_state(cfg)
        files = snapshot.values.get("files", {})
        (directory / "replay-files.json").write_text(json.dumps(files, sort_keys=True))
        if phase == "restore":
            (update,) = json.loads((directory / "queued-writes.json").read_text())
            assert files == update
            print("PROBE ONLY: exact native update restores file bytes and timestamps")
        else:
            assert "/artifact.txt" not in files, "counterexample did not reproduce"
            print(
                "CONFIRMED: settled content/status replay loses the queued file state"
            )


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]), sys.argv[2]))
