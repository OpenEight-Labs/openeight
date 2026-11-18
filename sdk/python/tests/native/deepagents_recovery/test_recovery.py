"""Native evidence only: these tests do not qualify SDK durable support."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

if os.name != "posix":
    pytest.skip("Native recovery probes require POSIX", allow_module_level=True)

pytest.importorskip("deepagents", reason="Native probes require the deepagents extra")
pytest.importorskip(
    "langgraph.checkpoint.sqlite.aio",
    reason="Native probes require the deepagents-durable extra",
)

HERE = Path(__file__).parent


def invoke(script, directory, phase, expected=0):
    result = subprocess.run(
        [sys.executable, str(HERE / script), str(directory), phase],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    return result


def test_child_interrupt_restarts_without_repeating_successful_sibling(tmp_path):
    invoke("probe.py", tmp_path, "start")
    before = json.loads((tmp_path / "start.json").read_text())
    assert before["interrupts"] and not (tmp_path / "effects.jsonl").exists()
    invoke("probe.py", tmp_path, "resume")
    after = json.loads((tmp_path / "resume.json").read_text())
    calls = [
        json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()
    ]
    business = [call for call in calls if call["name"] == "effect"]
    assert len(business) == 2 and business[0]["namespace"] == business[1]["namespace"]
    assert sum(call["label"] == "sibling" for call in calls) == 1
    assert sum(call["id"] == "delegate-worker" for call in calls) == 2
    assert after["next"] == [] and not after["interrupts"]
    assert json.loads((tmp_path / "effects.jsonl").read_text()) == {"value": "once"}
    assert after["files"]["/root.txt"] == before["files"]["/root.txt"]
    assert after["files"]["/sibling.txt"] == before["files"]["/sibling.txt"]
    assert after["files"]["/child.txt"]["content"] == "child artifact"
    assert (
        before["todos"]
        == after["todos"]
        == [{"content": "investigate", "status": "in_progress"}]
    )


def test_process_death_before_node_commit_exposes_file_settlement_gap(tmp_path):
    invoke("crash_gap.py", tmp_path, "kill", expected=73)
    (update,) = json.loads((tmp_path / "queued-writes.json").read_text())
    assert update["/artifact.txt"]["content"] == "must survive"
    assert (
        update["/artifact.txt"]["created_at"] == update["/artifact.txt"]["modified_at"]
    )
    saved = json.loads((tmp_path / "settlement.json").read_text())
    assert set(saved) == {"content", "status"} and saved["status"] == "success"
    invoke("crash_gap.py", tmp_path, "replay")
    assert json.loads((tmp_path / "replay-files.json").read_text()) == {}


def test_environment_matches_native_candidate():
    from importlib.metadata import version

    expected = {
        "deepagents": "0.7.23",
        "langchain": "1.4.3",
        "langchain-core": "1.6.7",
        "langgraph": "1.2.14",
        "langgraph-prebuilt": "1.1.0",
        "langgraph-checkpoint": "4.2.0",
        "langgraph-checkpoint-sqlite": "3.1.1",
        "aiosqlite": "0.22.1",
    }
    assert {k: version(k) for k in expected} == expected
    assert sys.implementation.name == "cpython"
    assert os.name == "posix"


def test_exact_native_file_update_receipt_is_sufficient_for_this_gap(tmp_path):
    invoke("crash_gap.py", tmp_path, "kill", expected=73)
    (update,) = json.loads((tmp_path / "queued-writes.json").read_text())
    invoke("crash_gap.py", tmp_path, "restore")
    assert json.loads((tmp_path / "replay-files.json").read_text()) == update


def test_concurrent_same_child_calls_keep_native_parent_identity_across_restarts(
    tmp_path,
):
    invoke("identity_probe.py", tmp_path, "start")
    invoke("identity_probe.py", tmp_path, "resume-A")
    assert [
        json.loads(s) for s in (tmp_path / "effects.jsonl").read_text().splitlines()
    ] == [{"parent": "parent-A"}]
    invoke("identity_probe.py", tmp_path, "resume-B")
    effects = [
        json.loads(s) for s in (tmp_path / "effects.jsonl").read_text().splitlines()
    ]
    assert effects == [{"parent": "parent-A"}, {"parent": "parent-B"}]
    identities = [
        json.loads(s) for s in (tmp_path / "identities.jsonl").read_text().splitlines()
    ]
    child = [v for v in identities if v["path"] == ["worker"]]
    assert {v["call_id"] for v in child} == {"same-child-call"}
    namespaces = {
        parent: {v["namespace"] for v in child if v["parent_id"] == parent}
        for parent in ("parent-A", "parent-B")
    }
    assert len(namespaces["parent-A"]) == len(namespaces["parent-B"]) == 1
    assert namespaces["parent-A"].isdisjoint(namespaces["parent-B"])


def test_swapped_parent_context_does_not_change_native_identity_or_execute(tmp_path):
    invoke("identity_probe.py", tmp_path, "start")
    invoke("identity_probe.py", tmp_path, "swap")
    assert not (tmp_path / "effects.jsonl").exists()
    assert (tmp_path / "rejected.jsonl").exists()
    prepared = [
        json.loads(line)
        for line in (tmp_path / "prepared.jsonl").read_text().splitlines()
    ]
    assert {"phase": "swap", "parent": "parent-A"} not in prepared


@pytest.mark.parametrize("tool", ["write_file", "write_todos", "task"])
def test_native_capture_restores_exact_generated_updates_after_process_death(
    tmp_path, tool
):
    invoke("capture_probe.py", tmp_path, "kill-" + tool, expected=78)
    invoke("capture_probe.py", tmp_path, "resume")


def test_committed_parent_receipt_does_not_overwrite_later_same_path_state(tmp_path):
    invoke("capture_probe.py", tmp_path, "kill-later", expected=78)
    invoke("capture_probe.py", tmp_path, "resume")


@pytest.mark.parametrize("boundary", ["restoration", "inspection"])
def test_recovery_process_death_preserves_later_same_path_bytes_and_timestamps(
    tmp_path, boundary
):
    invoke("capture_probe.py", tmp_path, "kill-later", expected=78)
    if boundary == "inspection":
        invoke("capture_probe.py", tmp_path, "resume")
    invoke(
        "capture_probe.py",
        tmp_path,
        "resume-kill-after-" + boundary,
        expected=80 if boundary == "restoration" else 81,
    )
    invoke("capture_probe.py", tmp_path, "resume")


def test_altered_replay_input_checkpoint_cannot_restore_earlier_same_path_state(
    tmp_path,
):
    invoke("capture_probe.py", tmp_path, "kill-later", expected=78)
    invoke("capture_probe.py", tmp_path, "resume")
    before = json.loads((tmp_path / "result.json").read_text())
    calls = (tmp_path / "handler_calls.jsonl").read_bytes()
    invoke("capture_probe.py", tmp_path, "resume-altered-input")
    after = json.loads((tmp_path / "result.json").read_text())
    assert before == after
    assert (tmp_path / "handler_calls.jsonl").read_bytes() == calls
    rejected = json.loads((tmp_path / "altered-input-rejected.json").read_text())
    assert rejected["original"] != rejected["altered"]


def test_child_completion_before_parent_receipt_does_not_reexecute_child(tmp_path):
    invoke("capture_probe.py", tmp_path, "kill-before-task-receipt", expected=79)
    invoke("capture_probe.py", tmp_path, "resume")


@pytest.mark.asyncio
async def test_cancelled_native_task_with_partial_file_output_is_never_committed(
    tmp_path,
):
    import asyncio
    from inspect import getclosurevars
    from types import SimpleNamespace

    from deepagents import create_deep_agent
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.errors import NodeCancelledError
    from probe import calls, model

    from openeight.deepagents._continuation import (
        DurableCallContext,
        durable_attempt_key,
    )
    from openeight.deepagents._deployment import canonical
    from openeight.deepagents._frameworks._generated import CapturedStateBackend
    from openeight.deepagents._frameworks._identity import NativeObservations
    from openeight.deepagents._frameworks.v1 import DeepAgentsV1
    from openeight.langchain._bindings import AdapterBindings
    from openeight.langchain._runtime import invocation_context

    compatibility = DeepAgentsV1()
    backend = CapturedStateBackend()
    invocation = SimpleNamespace(
        deployment_id="native-probe", thread_id="partial-output"
    )
    saved = {}

    class CancelAfterCapture(AgentMiddleware):
        async def awrap_tool_call(self, request, handler):
            native = compatibility.native_identity(
                request, invocation=invocation, child_path=()
            )
            context = DurableCallContext(
                (), native.checkpoint_namespace, None, "state_write"
            )
            key = durable_attempt_key(context, request.tool_call["id"])
            continuation = SimpleNamespace(
                deployment_id=invocation.deployment_id,
                thread_id=invocation.thread_id,
                decision_id="native-decision",
                child_path_json="[]",
                checkpoint_namespace=native.checkpoint_namespace,
                native_tool_call_id=request.tool_call["id"],
                attempt_key=key,
                parent_attempt_key=None,
                kind="state_write",
            )
            attempt = SimpleNamespace(
                deployment_id=invocation.deployment_id,
                thread_id=invocation.thread_id,
                decision_id="native-decision",
                tool_call_id=key,
                native_call_json=canonical(request.tool_call),
            )
            capture = await compatibility.capture_generated(
                request,
                handler,
                invocation=invocation,
                attempt=attempt,
                continuation=continuation,
            )
            attempt.result_json = canonical(
                {"content": capture.result.content, "status": capture.result.status}
            )
            saved.update(
                attempt=attempt, continuation=continuation, receipt=capture.receipt
            )
            raise asyncio.CancelledError

    async with AsyncSqliteSaver.from_conn_string(
        str(tmp_path / "partial.sqlite")
    ) as saver:
        graph = create_deep_agent(
            model(
                calls(
                    (
                        "partial",
                        "write_file",
                        {"file_path": "/partial.txt", "content": "partial"},
                    )
                )
            ),
            backend=backend,
            middleware=[CancelAfterCapture()],
            checkpointer=saver,
        )
        children = getclosurevars(
            graph.nodes["tools"].bound.tools_by_name["task"].coroutine
        ).nonlocals["subagent_graphs"]
        graphs = {(): graph, **{(name,): child for name, child in children.items()}}
        compatibility._graphs = graphs
        compatibility._backend = backend
        compatibility._bindings = AdapterBindings(
            {
                path: tuple(g.nodes["tools"].bound.tools_by_name.values())
                for path, g in graphs.items()
            },
            deep=True,
        )
        invocation.native_observations = NativeObservations(
            paths=graphs, thread_id=invocation.thread_id
        )
        invocation.owner = SimpleNamespace(_graph=graph)
        config = {"configurable": {"thread_id": invocation.thread_id}}
        with invocation_context(invocation), pytest.raises(NodeCancelledError):
            await graph.ainvoke(
                {"messages": [HumanMessage(content="go", id="native-command")]},
                config,
                durability="sync",
            )
        snapshot = await compatibility.read_checkpoint(graph, config)
        task = next(
            t
            for t in snapshot.native.tasks
            if t.namespace == saved["continuation"].checkpoint_namespace
        )
        assert task.task.result is not None and task.task.error is not None
        assert any(channel == "files" for _, channel, _ in task.pending_writes)
        replay = await compatibility.inspect_generated_replay(
            snapshot, invocation=invocation, **saved
        )
        assert replay.disposition == "unknown"
