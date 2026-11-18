"""Native identity evidence rejects malformed ancestry before durable preparation."""

import copy
from types import SimpleNamespace

import pytest

pytest.importorskip("deepagents")

from openeight import ConfigurationError
from openeight.deepagents._frameworks._identity import NativeObservations

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
CHECKPOINT = "33333333-3333-4333-8333-333333333333"


@pytest.mark.parametrize(
    "variant", ["missing", "object", "subclass", "pickle", "saver_subclass", "spoof"]
)
def test_durable_saver_rejects_unqualified_serializers_and_types(variant):
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from openeight.deepagents._frameworks.v1 import DeepAgentsV1

    class CustomSerializer(JsonPlusSerializer):
        pass

    class CustomSaver(AsyncSqliteSaver):
        pass

    saver_type = CustomSaver if variant == "saver_subclass" else AsyncSqliteSaver
    if variant == "spoof":
        saver_type = type(
            "AsyncSqliteSaver", (), {"__module__": AsyncSqliteSaver.__module__}
        )
    saver = object.__new__(saver_type)
    saver.serde = None
    if variant != "missing":
        saver.serde = (
            object()
            if variant == "object"
            else CustomSerializer(pickle_fallback=False)
            if variant == "subclass"
            else JsonPlusSerializer(pickle_fallback=variant == "pickle")
        )
    with pytest.raises(ConfigurationError) as error:
        DeepAgentsV1().validate_durable_saver(SimpleNamespace(checkpointer=saver))
    assert error.value.reason == "unsupported_checkpointer"


def test_durable_saver_accepts_exact_qualified_serializer():
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from openeight.deepagents._frameworks.v1 import DeepAgentsV1

    saver = object.__new__(AsyncSqliteSaver)
    saver.serde = JsonPlusSerializer(pickle_fallback=False)
    DeepAgentsV1().validate_durable_saver(SimpleNamespace(checkpointer=saver))


def config(*parts):
    namespace = "|".join("tools:" + part for part in parts)
    ancestors = [
        "|".join("tools:" + part for part in parts[:i]) for i in range(1, len(parts))
    ]
    return {
        "configurable": {
            "thread_id": "thread",
            "checkpoint_ns": namespace,
            "__pregel_task_id": parts[-1],
            "checkpoint_map": {name: CHECKPOINT for name in ["", *ancestors]},
        },
        "metadata": {"langgraph_checkpoint_ns": namespace, "langgraph_node": "tools"},
    }


def root(call_id="parent"):
    return {"id": call_id, "name": "task", "args": {"subagent_type": "worker"}}


def test_child_parent_comes_from_native_observation_not_context():
    evidence = NativeObservations(paths=[(), ("worker",)], thread_id="thread")
    evidence.observe(config(A), root(), child_path=())
    child = evidence.observe(
        config(A, B),
        {"id": "same", "name": "effect", "args": {}},
        child_path=("worker",),
    )
    assert child.parent_native_tool_call_id == "parent"
    assert child.ancestor_namespaces == ("tools:" + A,)
    assert (
        evidence.observe(
            config(A, B),
            {"id": "same", "name": "effect", "args": {}},
            child_path=("worker",),
        )
        == child
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c["configurable"].update(__pregel_task_id=A),
        lambda c: c["configurable"]["checkpoint_map"].pop("tools:" + A),
        lambda c: c["configurable"]["checkpoint_map"].update(foreign=CHECKPOINT),
        lambda c: c["metadata"].update(langgraph_node="model"),
        lambda c: c["metadata"].update(langgraph_checkpoint_ns="tools:" + A),
    ],
)
def test_altered_native_structure_rejects(mutate):
    evidence = NativeObservations(paths=[(), ("worker",)], thread_id="thread")
    evidence.observe(config(A), root(), child_path=())
    child = copy.deepcopy(config(A, B))
    mutate(child)
    with pytest.raises(ConfigurationError) as error:
        evidence.observe(
            child, {"id": "same", "name": "effect", "args": {}}, child_path=("worker",)
        )
    assert error.value.reason == "invalid_resume_correlation"


def test_conflicting_duplicate_native_observation_and_unobserved_parent_reject():
    evidence = NativeObservations(paths=[(), ("worker",)], thread_id="thread")
    with pytest.raises(ConfigurationError):
        evidence.observe(
            config(A, B),
            {"id": "same", "name": "effect", "args": {}},
            child_path=("worker",),
        )
    evidence.observe(config(A), root("parent-A"), child_path=())
    with pytest.raises(ConfigurationError):
        evidence.observe(config(A), root("parent-B"), child_path=())
