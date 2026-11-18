"""Exercise the qualified capture mechanism across real native process death."""

import asyncio
import hashlib
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from deepagents import create_deep_agent
from langchain.agents.middleware import AgentMiddleware, TodoListMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.config import get_config
from probe import ScriptModel, append, calls, model

from openeight._framework._types import NativeToolResult
from openeight.deepagents._continuation import DurableCallContext, durable_attempt_key
from openeight.deepagents._frameworks._generated import (
    CapturedStateBackend,
    capture_native_state,
)
from openeight.deepagents._frameworks._identity import NativeObservations
from openeight.deepagents._frameworks.v1 import DeepAgentsV1
from openeight.deepagents.durable import GeneratedStateReceipt
from openeight.langchain._frameworks.v1 import LangChainV1
from openeight.langchain._runtime import invocation_context


class CaptureGate(AgentMiddleware):
    def __init__(self, directory, phase, backend, invocation, path):
        self.directory, self.phase, self.backend = directory, phase, backend
        self.compatibility = LangChainV1()
        self.invocation, self.path = invocation, path

    async def awrap_tool_call(self, request, handler):
        self.invocation.native_observations.observe(
            get_config(), request.tool_call, child_path=self.path
        )
        config = get_config()["configurable"]
        key = hashlib.sha256(
            json.dumps([config["checkpoint_ns"], request.tool_call["id"]]).encode()
        ).hexdigest()
        path = self.directory / (key + ".receipt.json")
        if path.exists():
            saved = json.loads(path.read_text())
            assert saved["namespace"] == config["checkpoint_ns"]
            enclosing = (
                config["checkpoint_ns"].rsplit("|", 1)[0]
                if "|" in config["checkpoint_ns"]
                else ""
            )
            assert saved["input_checkpoint_id"] == config["checkpoint_map"][enclosing]
            replay = self.invocation.replays[key]
            assert replay.disposition == "replayable"
            restored = self.invocation.compatibility.generated_result(
                NativeToolResult(**saved["result"]),
                invocation=self.invocation,
                continuation=SimpleNamespace(**saved["continuation"]),
                receipt=GeneratedStateReceipt(**saved["receipt"]),
                replay=replay,
            )
            if (
                self.phase == "resume-kill-after-restoration"
                and saved["result"]["tool_call_id"] == "later"
            ):
                os._exit(80)
            return restored
        append(
            self.directory / "handler_calls.jsonl",
            {"id": request.tool_call["id"], "phase": self.phase},
        )
        captured = await capture_native_state(
            request,
            handler,
            compatibility=self.compatibility,
            invocation=self.invocation,
        )
        saved = {
            "result": asdict(captured.result),
            "input_checkpoint_id": captured.input_checkpoint_id,
            "update_json": captured.update_json,
            "namespace": config["checkpoint_ns"],
            "continuation": {
                "deployment_id": self.invocation.deployment_id,
                "thread_id": self.invocation.thread_id,
                "decision_id": "probe:" + request.tool_call["id"],
                "child_path_json": json.dumps(list(self.path)),
                "checkpoint_namespace": config["checkpoint_ns"],
                "native_tool_call_id": request.tool_call["id"],
            },
            "receipt": {
                "deployment_id": self.invocation.deployment_id,
                "thread_id": self.invocation.thread_id,
                "decision_id": "probe:" + request.tool_call["id"],
                "format_version": 1,
                "input_checkpoint_id": captured.input_checkpoint_id,
                "update_json": captured.update_json,
            },
            "native_call_json": json.dumps(request.tool_call),
        }
        kind = "delegation" if request.tool_call["name"] == "task" else "state_write"
        parent_key = None
        if self.path:
            parent_namespace = config["checkpoint_ns"].rsplit("|", 1)[0]
            parent = self.invocation.native_observations.observations[parent_namespace]
            parent_key = durable_attempt_key(
                DurableCallContext((), parent_namespace, None, "delegation"),
                parent.native_call_id,
            )
        context = DurableCallContext(
            self.path, config["checkpoint_ns"], parent_key, kind
        )
        saved["continuation"].update(
            kind=kind,
            parent_attempt_key=parent_key,
            attempt_key=durable_attempt_key(context, request.tool_call["id"]),
        )
        if (
            self.phase == "kill-before-task-receipt"
            and request.tool_call["name"] == "task"
        ):
            os._exit(79)
        path.write_text(json.dumps(saved, sort_keys=True))
        if (
            self.phase == "kill-" + request.tool_call["name"]
            or self.phase == "kill-later"
            and request.tool_call["id"] == "later"
        ):
            os._exit(78)
        return captured.native_result


class NoRepeatChildModel(ScriptModel):
    def _generate(self, messages, *args, **kwargs):
        assert not any(
            isinstance(m, AIMessage) and m.content == "done" for m in messages
        ), "completed child model reexecuted"
        return super()._generate(messages, *args, **kwargs)


async def run(directory, phase):
    backend = CapturedStateBackend()
    invocation = SimpleNamespace(
        deployment_id="native-probe",
        thread_id="generated-capture",
        native_observations=NativeObservations(
            paths=[(), ("worker",)], thread_id="generated-capture"
        ),
    )
    gate = CaptureGate(directory, phase, backend, invocation, ())
    child_gate = CaptureGate(directory, phase, backend, invocation, ("worker",))
    child = dict(
        name="worker",
        description="work",
        tools=[],
        system_prompt="work",
        model=NoRepeatChildModel(
            responses=[
                calls(
                    (
                        "child-file",
                        "write_file",
                        {"file_path": "/child.txt", "content": "child"},
                    )
                ),
                AIMessage(content="done"),
            ]
        ),
        middleware=[child_gate],
    )
    script = model(
        calls(
            (
                "todo",
                "write_todos",
                {"todos": [{"content": "keep", "status": "in_progress"}]},
            )
        ),
        calls(
            ("file", "write_file", {"file_path": "/report.txt", "content": "original"})
        ),
        calls(("task", "task", {"description": "child", "subagent_type": "worker"})),
        calls(
            ("later", "write_file", {"file_path": "/report.txt", "content": "later"})
        ),
    )
    async with AsyncSqliteSaver.from_conn_string(
        str(directory / "checkpoints.sqlite")
    ) as saver:
        graph = create_deep_agent(
            script,
            tools=[],
            backend=backend,
            subagents=[child],
            middleware=[TodoListMiddleware(), gate],
            checkpointer=saver,
        )
        cfg = {"configurable": {"thread_id": "generated-capture"}}
        from inspect import getclosurevars

        compatibility = DeepAgentsV1()
        children = getclosurevars(
            graph.nodes["tools"].bound.tools_by_name["task"].coroutine
        ).nonlocals["subagent_graphs"]
        graphs = {(): graph, **{(name,): child for name, child in children.items()}}
        compatibility._graphs = graphs
        compatibility._backend = backend
        compatibility._bindings = SimpleNamespace(
            resolve=lambda path, name: graphs[path]
            .nodes["tools"]
            .bound.tools_by_name.get(name)
        )
        invocation.owner = SimpleNamespace(_graph=graph)
        invocation.compatibility = compatibility
        invocation.replays = {}
        if phase.startswith("resume"):
            snapshot = await compatibility.read_checkpoint(graph, cfg)
            invocation.native_observations = snapshot.native.observations
            later = next(
                (
                    json.loads(p.read_text())
                    for p in directory.glob("*.receipt.json")
                    if json.loads(p.read_text())["result"]["tool_call_id"] == "later"
                ),
                None,
            )
            for file in directory.glob("*.receipt.json"):
                saved = json.loads(file.read_text())
                receipt = dict(saved["receipt"])
                altered = (
                    phase == "resume-altered-input"
                    and saved["result"]["tool_call_id"] == "file"
                )
                if altered:
                    assert later is not None
                    assert (
                        receipt["input_checkpoint_id"] != later["input_checkpoint_id"]
                    )
                    receipt["input_checkpoint_id"] = later["input_checkpoint_id"]
                replay = await compatibility.inspect_generated_replay(
                    snapshot,
                    invocation=invocation,
                    attempt=SimpleNamespace(
                        decision_id=saved["receipt"]["decision_id"],
                        deployment_id=invocation.deployment_id,
                        thread_id=invocation.thread_id,
                        tool_call_id=saved["continuation"]["attempt_key"],
                        native_call_json=saved["native_call_json"],
                        result_json=json.dumps(
                            {k: saved["result"][k] for k in ("content", "status")}
                        ),
                    ),
                    continuation=SimpleNamespace(**saved["continuation"]),
                    receipt=GeneratedStateReceipt(**receipt),
                )
                if altered:
                    assert replay.disposition == "unknown"
                    (directory / "altered-input-rejected.json").write_text(
                        json.dumps(
                            {
                                "original": saved["input_checkpoint_id"],
                                "altered": receipt["input_checkpoint_id"],
                            }
                        )
                    )
                    continue
                assert replay.disposition != "unknown", (
                    saved["result"]["tool_call_id"],
                    replay,
                )
                if (
                    phase == "resume-kill-after-inspection"
                    and saved["result"]["tool_call_id"] == "file"
                ):
                    assert replay.disposition == "already_committed"
                    assert (
                        snapshot.native.root.values["files"]["/report.txt"]
                        == json.loads(later["update_json"])["files"]["/report.txt"]
                    )
                    os._exit(81)
                invocation.replays[file.name.removesuffix(".receipt.json")] = replay
        with invocation_context(invocation):
            if phase != "resume-altered-input":
                await graph.ainvoke(
                    {"messages": [HumanMessage(content="go", id="native-command")]}
                    if phase.startswith("kill-")
                    else None,
                    cfg,
                    durability="sync",
                )
        snapshot = await graph.aget_state(cfg)
        data = {
            "files": snapshot.values.get("files", {}),
            "todos": snapshot.values.get("todos", []),
        }
        (directory / "result.json").write_text(json.dumps(data, sort_keys=True))
        assert data["files"]["/report.txt"]["content"] == "later"
        assert data["files"]["/child.txt"]["content"] == "child"
        assert data["todos"] == [{"content": "keep", "status": "in_progress"}]
        saved = [json.loads(p.read_text()) for p in directory.glob("*.receipt.json")]
        assert all(item["result"]["status"] == "success" for item in saved)
        first = next(item for item in saved if item["result"]["tool_call_id"] == "file")
        later = next(
            item for item in saved if item["result"]["tool_call_id"] == "later"
        )
        original = json.loads(first["update_json"])["files"]["/report.txt"]
        latest = json.loads(later["update_json"])["files"]["/report.txt"]
        assert data["files"]["/report.txt"] == latest
        assert latest["created_at"] == original["created_at"]
        handler_calls = [
            json.loads(line)
            for line in (directory / "handler_calls.jsonl").read_text().splitlines()
        ]
        assert sum(call["id"] == "child-file" for call in handler_calls) == 1
        assert sum(call["id"] == "file" for call in handler_calls) == 1
        assert sum(call["id"] == "later" for call in handler_calls) == 1


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]), sys.argv[2]))
