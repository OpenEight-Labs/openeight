"""Qualified native state capture; journal settlement remains shared SDK work."""

import threading
from contextvars import ContextVar
from dataclasses import dataclass, field

from deepagents.backends import StateBackend
from langgraph.config import get_config
from langgraph.types import Command

from openeight._framework._types import NativeToolResult
from openeight.errors import ConfigurationError

from ._state import decode_state_update, encode_state_update

_CAPTURE = ContextVar("openeight_native_state_capture", default=None)


@dataclass(frozen=True)
class NativeStateCapture:
    invocation: object
    deployment_id: str
    thread_id: str
    native_call_id: str
    task_id: str
    checkpoint_map: tuple
    namespace: str
    input_checkpoint_id: str
    files: dict = field(default_factory=dict)
    lock: object = field(default_factory=threading.Lock)

    def record_files(self, update):
        from openeight.langchain._runtime import CURRENT_INVOCATION

        config = get_config()
        selectors = config["configurable"]
        if (
            CURRENT_INVOCATION.get() is not self.invocation
            or self.invocation.deployment_id != self.deployment_id
            or self.invocation.thread_id != self.thread_id
        ):
            raise ConfigurationError("invalid_resume_correlation")
        current = self.invocation.native_observations.validate_runtime(config)
        if selectors.get("checkpoint_ns") != self.namespace:
            if self.namespace not in current.identity.ancestor_namespaces:
                raise ConfigurationError("invalid_resume_correlation")
            return
        if (
            tuple(sorted(selectors["checkpoint_map"].items())) != self.checkpoint_map
            or selectors.get("__pregel_task_id") != self.task_id
            or current.native_call_id != self.native_call_id
        ):
            raise ConfigurationError("invalid_resume_correlation")
        enclosing = self.namespace.rsplit("|", 1)[0] if "|" in self.namespace else ""
        if (
            selectors.get("checkpoint_map", {}).get(enclosing)
            != self.input_checkpoint_id
        ):
            raise ConfigurationError("durable_checkpoint_conflict")
        frozen = decode_state_update(encode_state_update({"files": update}))["files"]
        with self.lock:
            self.files.update(frozen)


class CapturedStateBackend(StateBackend):
    """Preserve StateBackend semantics while observing the qualified file channel."""

    def _send_files_update(self, update):
        capture = _CAPTURE.get()
        if capture is not None:
            capture.record_files(update)
        return super()._send_files_update(update)


@dataclass(frozen=True)
class CapturedNativeState:
    result: NativeToolResult
    input_checkpoint_id: str
    update_json: str
    native_result: object


async def capture_native_state(request, handler, *, compatibility, invocation):
    selectors = get_config()["configurable"]
    namespace = selectors["checkpoint_ns"]
    enclosing = namespace.rsplit("|", 1)[0] if "|" in namespace else ""
    checkpoint_id = selectors["checkpoint_map"][enclosing]
    from openeight.langchain._runtime import CURRENT_INVOCATION

    current = invocation.native_observations.validate_runtime(get_config())
    if (
        CURRENT_INVOCATION.get() is not invocation
        or selectors.get("thread_id") != invocation.thread_id
        or current.native_call_id != request.tool_call["id"]
        or getattr(request.runtime, "tool_call_id", None) != request.tool_call["id"]
    ):
        raise ConfigurationError("invalid_resume_correlation")
    capture = NativeStateCapture(
        invocation,
        invocation.deployment_id,
        invocation.thread_id,
        request.tool_call["id"],
        selectors["__pregel_task_id"],
        tuple(sorted(selectors["checkpoint_map"].items())),
        namespace,
        checkpoint_id,
    )
    token = _CAPTURE.set(capture)
    try:
        native = await handler(request)
    finally:
        _CAPTURE.reset(token)
    update = {}
    if isinstance(native, Command):
        if (
            native.graph is not None
            or native.goto
            or native.resume is not None
            or type(native.update) is not dict
        ):
            raise ConfigurationError("durable_reconciliation_required")
        update = dict(native.update)
        messages = update.pop("messages", None)
        if type(messages) is not list or len(messages) != 1:
            raise ConfigurationError("invalid_tool_result")
        message = messages[0]
    else:
        message = native
    result = compatibility.tool_result(message)
    if (
        result.tool_call_id != request.tool_call["id"]
        or result.tool_name != request.tool_call["name"]
    ):
        raise ConfigurationError("invalid_tool_result")
    with capture.lock:
        if capture.files:
            if "files" in update:
                raise ConfigurationError("durable_reconciliation_required")
            update["files"] = dict(capture.files)
    return CapturedNativeState(
        result, checkpoint_id, encode_state_update(update), native
    )


def restore_native_state(captured, *, compatibility, backend):
    """Called only after the original pending task and authority are validated."""
    update = decode_state_update(captured.update_json)
    message = compatibility.tool_message(captured.result)
    if captured.result.tool_name in {"write_file", "edit_file", "delete"}:
        if set(update) - {"files"}:
            raise ConfigurationError("durable_reconciliation_required")
        if "files" in update:
            backend._send_files_update(update["files"])
        return message
    if captured.result.tool_name not in {"write_todos", "task"}:
        raise ConfigurationError("durable_reconciliation_required")
    return Command(update={**update, "messages": [message]})
