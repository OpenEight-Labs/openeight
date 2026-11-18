"""Native partial output and callable metadata cannot authorize recovery."""

import copy
import functools
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("deepagents")

from deepagents.backends import StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.messages import ToolMessage
from langgraph._internal._constants import ERROR, INTERRUPT, RESUME

from openeight.deepagents._frameworks._generated import CapturedStateBackend
from openeight.deepagents._frameworks._generated_tools import generated_kind
from openeight.deepagents._frameworks._replay import saved_completion
from openeight.deepagents._frameworks._state import encode_state_update
from openeight.langchain._frameworks.v1 import LangChainV1


class ReplayCompatibility(LangChainV1):
    def tool_output_routes(self, child_path):
        return frozenset({"branch:to:model"})


UPDATE = {
    "/file": {
        "content": "first",
        "encoding": "utf-8",
        "created_at": "2026-11-04T00:00:00Z",
        "modified_at": "2026-11-04T00:00:00Z",
    }
}


def projection(*, error=None, controls=(), complete=True):
    writes = [("task", channel, value) for channel, value in controls]
    if complete:
        writes += [
            ("task", "files", copy.deepcopy(UPDATE)),
            (
                "task",
                "messages",
                [ToolMessage(content="saved", name="write_file", tool_call_id="call")],
            ),
            ("task", "branch:to:model", None),
        ]
    return SimpleNamespace(
        child_path=(),
        pending_writes=tuple(writes),
        task=SimpleNamespace(id="task", error=error),
        call={"name": "write_file", "id": "call"},
    )


def disposition(task):
    return saved_completion(
        ReplayCompatibility(),
        task,
        result_json=json.dumps({"content": "saved", "status": "success"}),
        receipt=SimpleNamespace(update_json=encode_state_update({"files": UPDATE})),
    )


def test_exact_original_output_is_completed_independent_of_later_aggregate_state():
    assert disposition(projection()) == "already_committed"


@pytest.mark.parametrize("error", ["cancelled", RuntimeError("failed")])
def test_partial_file_output_with_error_never_counts_as_committed(error):
    assert disposition(projection(error=error, controls=[(ERROR, error)])) == "unknown"


@pytest.mark.parametrize("channel", [INTERRUPT, RESUME])
def test_interrupt_or_resume_alone_is_pending(channel):
    assert (
        disposition(projection(complete=False, controls=[(channel, [])])) == "pending"
    )


def test_partial_file_output_without_terminal_message_is_uncertain():
    task = projection()
    task.pending_writes = task.pending_writes[:1]
    assert disposition(task) == "unknown"


def test_changed_saved_result_or_unknown_channel_is_uncertain():
    task = projection()
    task.pending_writes += (("task", "unqualified", {}),)
    assert disposition(task) == "unknown"
    task = projection()
    task.pending_writes[1][2][0].content = "different"
    assert disposition(task) == "unknown"


@pytest.mark.parametrize("name", ["write_file", "edit_file", "delete"])
def test_generated_file_recognition_checks_code_owner_and_backend(name):
    backend = CapturedStateBackend()
    middleware = FilesystemMiddleware(backend=backend)
    tool = next(t for t in middleware.tools if t.name == name)
    binding = SimpleNamespace(native_name=name, tool=tool)
    assert generated_kind(binding, backend=backend, graphs={}) == "state_write"
    assert generated_kind(binding, backend=StateBackend(), graphs={}) is None
    original = tool.coroutine

    @functools.wraps(original)
    async def wrapper(*args, **kwargs):
        return await original(*args, **kwargs)

    tool.coroutine = wrapper
    assert generated_kind(binding, backend=backend, graphs={}) is None


def test_generated_todo_requires_exact_functions_even_with_same_tool_name():
    tool = TodoListMiddleware().tools[0]
    binding = SimpleNamespace(native_name="write_todos", tool=tool.model_copy())
    assert generated_kind(binding, backend=None, graphs={}) == "state_write"
    original = binding.tool.coroutine

    @functools.wraps(original)
    async def wrapper(*args, **kwargs):
        return await original(*args, **kwargs)

    binding.tool.coroutine = wrapper
    assert generated_kind(binding, backend=None, graphs={}) is None


def test_generated_task_requires_exact_native_closures_and_child_graphs():
    from deepagents.middleware.subagents import _build_task_tool

    tool = _build_task_tool([])
    binding = SimpleNamespace(native_name="task", tool=tool, child_path=())
    assert generated_kind(binding, backend=None, graphs={(): object()}) == "delegation"
    assert (
        generated_kind(binding, backend=None, graphs={("missing",): object()}) is None
    )
    original = tool.coroutine

    @functools.wraps(original)
    async def wrapper(*args, **kwargs):
        return await original(*args, **kwargs)

    tool.coroutine = wrapper
    assert generated_kind(binding, backend=None, graphs={(): object()}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record,field,value",
    [
        ("attempt", "deployment_id", "foreign"),
        ("attempt", "thread_id", "foreign"),
        ("attempt", "tool_call_id", "foreign-key"),
        ("continuation", "attempt_key", "foreign-key"),
        ("continuation", "native_tool_call_id", "foreign-call"),
        ("continuation", "deployment_id", "foreign"),
        ("continuation", "thread_id", "foreign"),
        ("receipt", "format_version", True),
    ],
)
async def test_foreign_attempt_or_continuation_rejects_before_any_saver_read(
    record, field, value
):
    from openeight import ConfigurationError
    from openeight.deepagents._continuation import (
        DurableCallContext,
        durable_attempt_key,
    )
    from openeight.deepagents._frameworks._replay import inspect_replay

    namespace = "tools:11111111-1111-4111-8111-111111111111"
    key = durable_attempt_key(
        DurableCallContext((), namespace, None, "state_write"), "call"
    )
    records = {
        "attempt": SimpleNamespace(
            deployment_id="deployment",
            thread_id="thread",
            decision_id="same-decision",
            tool_call_id=key,
            native_call_json=json.dumps(
                {"id": "call", "name": "write_file", "args": {}}
            ),
        ),
        "continuation": SimpleNamespace(
            deployment_id="deployment",
            thread_id="thread",
            decision_id="same-decision",
            child_path_json="[]",
            checkpoint_namespace=namespace,
            parent_attempt_key=None,
            kind="state_write",
            native_tool_call_id="call",
            attempt_key=key,
        ),
        "receipt": SimpleNamespace(
            deployment_id="deployment",
            thread_id="thread",
            decision_id="same-decision",
            format_version=1,
        ),
    }
    setattr(records[record], field, value)
    invocation = SimpleNamespace(deployment_id="deployment", thread_id="thread")
    # No owner or saver exists; a same-Decision foreign record must reject first.
    with pytest.raises(ConfigurationError) as caught:
        await inspect_replay(None, None, invocation=invocation, **records)
    assert caught.value.reason == "durable_attempt_conflict"


@pytest.mark.parametrize(
    "name", ["write_file", "edit_file", "delete", "write_todos", "task"]
)
def test_read_only_readiness_cannot_override_qualified_native_mutation(name):
    from uuid import uuid4

    from deepagents.middleware.subagents import _build_task_tool

    from openeight import ConfigurationError
    from openeight.deepagents._admission import _contract
    from openeight.deepagents._frameworks.v1 import DeepAgentsV1
    from openeight.langchain._bindings import AdapterBindings
    from openeight.tools import ToolReadiness

    backend = CapturedStateBackend()
    tools = [
        *FilesystemMiddleware(backend=backend).tools,
        *TodoListMiddleware().tools,
        _build_task_tool([]),
    ]
    tool = next(t for t in tools if t.name == name)
    binding = AdapterBindings({(): (tool,)}, deep=True).resolve((), name)
    compatibility = DeepAgentsV1()
    compatibility._backend, compatibility._graphs = backend, {(): object()}
    owner = SimpleNamespace(_compatibility=compatibility)
    tool_id, version_id = str(uuid4()), str(uuid4())
    readiness = ToolReadiness(
        None,
        binding.facts.name,
        tool_id,
        version_id,
        "registered",
        False,
        True,
        True,
        "read_only",
    )
    with pytest.raises(ConfigurationError) as caught:
        _contract(owner, binding, readiness, tool_id=tool_id, version_id=version_id)
    assert caught.value.reason == "unsupported_tool_definition"
