"""Generated receipts never capture state from a foreign native task or owner."""

import copy
from types import SimpleNamespace

import pytest

pytest.importorskip("deepagents")

from openeight import ConfigurationError
from openeight.deepagents._frameworks import _generated
from openeight.deepagents._frameworks._identity import NativeObservations
from openeight.langchain._runtime import CURRENT_INVOCATION

TASK = "11111111-1111-4111-8111-111111111111"
CHECKPOINT = "22222222-2222-4222-8222-222222222222"
UPDATE = {
    "/report.txt": {
        "content": "report",
        "encoding": "utf-8",
        "created_at": "2026-11-04T00:00:00Z",
        "modified_at": "2026-11-04T00:00:00Z",
    }
}


def setup(monkeypatch):
    config = {
        "configurable": {
            "thread_id": "thread",
            "checkpoint_ns": "tools:" + TASK,
            "__pregel_task_id": TASK,
            "checkpoint_map": {"": CHECKPOINT},
        },
        "metadata": {
            "langgraph_checkpoint_ns": "tools:" + TASK,
            "langgraph_node": "tools",
        },
    }
    evidence = NativeObservations(paths=[()], thread_id="thread")
    evidence.observe(
        config, {"id": "native-call", "name": "write_file", "args": {}}, child_path=()
    )
    invocation = SimpleNamespace(
        deployment_id="deployment", thread_id="thread", native_observations=evidence
    )
    capture = _generated.NativeStateCapture(
        invocation,
        "deployment",
        "thread",
        "native-call",
        TASK,
        (("", CHECKPOINT),),
        "tools:" + TASK,
        CHECKPOINT,
    )
    monkeypatch.setattr(_generated, "get_config", lambda: config)
    return config, invocation, capture


def test_generated_capture_freezes_exact_state_without_aliasing(monkeypatch):
    config, invocation, capture = setup(monkeypatch)
    token = CURRENT_INVOCATION.set(invocation)
    try:
        update = copy.deepcopy(UPDATE)
        capture.record_files(update)
        update["/report.txt"]["content"] = "changed later"
        assert capture.files == UPDATE
    finally:
        CURRENT_INVOCATION.reset(token)


@pytest.mark.parametrize(
    "field,value",
    [
        ("thread_id", "foreign"),
        ("__pregel_task_id", "33333333-3333-4333-8333-333333333333"),
        ("checkpoint_map", {"": "changed-checkpoint"}),
    ],
)
def test_foreign_native_config_never_enters_receipt(monkeypatch, field, value):
    config, invocation, capture = setup(monkeypatch)
    config["configurable"][field] = value
    token = CURRENT_INVOCATION.set(invocation)
    try:
        with pytest.raises(ConfigurationError):
            capture.record_files(UPDATE)
        assert capture.files == {}
    finally:
        CURRENT_INVOCATION.reset(token)


@pytest.mark.parametrize("field", ["deployment_id", "thread_id"])
def test_changed_command_owner_never_enters_receipt(monkeypatch, field):
    config, invocation, capture = setup(monkeypatch)
    setattr(invocation, field, "foreign")
    token = CURRENT_INVOCATION.set(invocation)
    try:
        with pytest.raises(ConfigurationError):
            capture.record_files(UPDATE)
        assert capture.files == {}
    finally:
        CURRENT_INVOCATION.reset(token)


def test_foreign_invocation_context_cannot_write_same_namespace(monkeypatch):
    config, invocation, capture = setup(monkeypatch)
    token = CURRENT_INVOCATION.set(
        SimpleNamespace(deployment_id="deployment", thread_id="thread")
    )
    try:
        with pytest.raises(ConfigurationError):
            capture.record_files(UPDATE)
        assert capture.files == {}
    finally:
        CURRENT_INVOCATION.reset(token)
