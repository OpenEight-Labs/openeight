"""Prove generated completion from its original native input and saved writes."""

import json

from langgraph._internal._constants import ERROR, ERROR_SOURCE_NODE, INTERRUPT, RESUME
from langgraph.pregel._task_status import read_task_statuses

from openeight._framework._types import GeneratedReplayState, NativeToolResult
from openeight.errors import ConfigurationError

from .._continuation import DurableCallContext, durable_attempt_key
from .._deployment import parse
from ._checkpoints import read_deep_checkpoint
from ._identity import _namespace_parts
from ._state import encode_state_update


def native_completion(compatibility, task):
    """Partial output on failed/cancelled tasks is never successful completion."""
    status = read_task_statuses(task.pending_writes).get(task.task.id)
    if status is None or not status.finished:
        if task.task.error is not None or any(
            channel not in {INTERRUPT, RESUME} for _, channel, _ in task.pending_writes
        ):
            return "unknown", None, None
        return "pending", None, None
    if (
        status.error is not None
        or task.task.error is not None
        or any(
            channel in {ERROR, ERROR_SOURCE_NODE}
            for _, channel, _ in task.pending_writes
        )
    ):
        return "unknown", None, None
    update, messages = {}, []
    try:
        for channel, value in status.output:
            if channel == "messages":
                if type(value) is not list:
                    return "unknown", None, None
                messages.extend(value)
            elif channel == "files":
                if type(value) is not dict:
                    return "unknown", None, None
                update.setdefault("files", {}).update(value)
            elif channel == "todos":
                if "todos" in update:
                    return "unknown", None, None
                update["todos"] = value
            elif (
                channel not in compatibility.tool_output_routes(task.child_path)
                or value is not None
            ):
                return "unknown", None, None
        if len(messages) != 1:
            return "unknown", None, None
        result = compatibility.tool_result(messages[0])
        if (
            result.tool_call_id != task.call["id"]
            or result.tool_name != task.call["name"]
            or len(result.content.encode("utf-8")) > 65536
        ):
            return "unknown", None, None
        update_json = encode_state_update(update)
    except (ConfigurationError, ValueError, TypeError, KeyError, AttributeError):
        return "unknown", None, None
    return "already_committed", result, update_json


def saved_completion(compatibility, task, *, result_json, receipt):
    disposition, result, update_json = native_completion(compatibility, task)
    if disposition != "already_committed":
        return disposition
    try:
        frozen = json.loads(result_json)
        if type(frozen) is not dict or set(frozen) != {"content", "status"}:
            return "unknown"
        expected = NativeToolResult(
            task.call["id"], task.call["name"], frozen["content"], frozen["status"]
        )
        if result != expected or update_json != receipt.update_json:
            return "unknown"
    except (ValueError, KeyError, TypeError):
        return "unknown"
    return "already_committed"


async def _ancestor(saver, *, thread, namespace, checkpoint_id):
    """Follow this namespace's actual parent links; no timestamp/order inference."""
    config = {"configurable": {"thread_id": thread, "checkpoint_ns": namespace}}
    seen = set()
    for _ in range(10000):
        saved = await saver.aget_tuple(config)
        if saved is None:
            return False
        selectors = saved.config.get("configurable", {})
        identifier = selectors.get("checkpoint_id")
        if (
            selectors.get("thread_id") != thread
            or selectors.get("checkpoint_ns") != namespace
            or not identifier
            or identifier in seen
        ):
            return False
        if identifier == checkpoint_id:
            return True
        seen.add(identifier)
        config = saved.parent_config
        if config is None:
            return False
    return False


async def inspect_replay(
    compatibility, snapshot, *, invocation, attempt, continuation, receipt
):
    unknown = GeneratedReplayState("unknown", None)
    try:
        path = parse(continuation.child_path_json)
        original = json.loads(attempt.native_call_json)
        parts = _namespace_parts(continuation.checkpoint_namespace)
        context = DurableCallContext(
            tuple(path),
            continuation.checkpoint_namespace,
            continuation.parent_attempt_key,
            continuation.kind,
        )
        if (
            type(path) is not list
            or len(path) > 16
            or any(type(p) is not str or not p for p in path)
            or type(original) is not dict
            or original.get("id") != continuation.native_tool_call_id
            or continuation.kind not in {"state_write", "delegation"}
            or (len(parts) == 1) != (continuation.parent_attempt_key is None)
            or (not path) != (len(parts) == 1)
            or attempt.tool_call_id != continuation.attempt_key
            or durable_attempt_key(context, continuation.native_tool_call_id)
            != continuation.attempt_key
        ):
            raise ValueError
    except (ConfigurationError, ValueError, TypeError, AttributeError):
        raise ConfigurationError("durable_attempt_conflict") from None
    if (
        attempt.deployment_id != invocation.deployment_id
        or attempt.thread_id != invocation.thread_id
        or type(receipt.format_version) is not int
        or receipt.deployment_id != invocation.deployment_id
        or receipt.thread_id != invocation.thread_id
        or receipt.decision_id != attempt.decision_id
        or continuation.decision_id != attempt.decision_id
        or continuation.deployment_id != receipt.deployment_id
        or continuation.thread_id != receipt.thread_id
        or receipt.format_version != 1
    ):
        raise ConfigurationError("durable_attempt_conflict")
    namespace = continuation.checkpoint_namespace
    enclosing = namespace.rsplit("|", 1)[0] if "|" in namespace else ""
    saver = invocation.owner._graph.checkpointer
    config = {
        "configurable": {
            "thread_id": invocation.thread_id,
            "checkpoint_ns": enclosing,
            "checkpoint_id": receipt.input_checkpoint_id,
        }
    }
    saved = await saver.aget_tuple(config)
    if saved is None or type(saved.metadata.get("parents")) is not dict:
        return unknown
    ids = {**saved.metadata["parents"], enclosing: receipt.input_checkpoint_id}
    if "" not in ids:
        return unknown
    for parent_namespace, identifier in ids.items():
        if not await _ancestor(
            saver,
            thread=invocation.thread_id,
            namespace=parent_namespace,
            checkpoint_id=identifier,
        ):
            return unknown
    try:
        historical = await read_deep_checkpoint(
            compatibility, invocation.owner._graph, config, checkpoint_ids=ids
        )
    except ConfigurationError:
        return unknown
    tasks = [t for t in historical.native.tasks if t.namespace == namespace]
    if len(tasks) != 1:
        return unknown
    task = tasks[0]
    if (
        list(task.child_path) != path
        or task.call != original
        or task.call.get("id") != continuation.native_tool_call_id
        or task.input_checkpoint_id != receipt.input_checkpoint_id
    ):
        return unknown
    disposition = saved_completion(
        compatibility, task, result_json=attempt.result_json, receipt=receipt
    )
    if disposition == "pending":
        current = [t for t in snapshot.native.tasks if t.namespace == namespace]
        if (
            len(current) != 1
            or current[0].input_checkpoint_id != receipt.input_checkpoint_id
        ):
            return unknown
        disposition = "replayable"
    return GeneratedReplayState(disposition, task if disposition != "unknown" else None)
