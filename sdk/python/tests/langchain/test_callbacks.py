from uuid import uuid4
from test_observation_facade import bind
from test_framework_boundary import fixture
from openeight.langchain._callbacks import ObservationCallbacks


def test_duplicate_callbacks_and_hierarchy_keep_stable_ids(bind):
    owner = bind(fixture()[0])
    invocation = owner._admit_invocation({}, "invoke")
    callback = ObservationCallbacks(invocation)
    parent, child = uuid4(), uuid4()
    callback.on_chain_start({}, {}, run_id=parent)
    callback.on_chain_start({}, {}, run_id=parent)
    callback.on_tool_start(
        {"name": "echo"}, "secret", run_id=child, parent_run_id=parent
    )
    callback.on_tool_end("secret", run_id=child)
    callback.on_tool_end("secret", run_id=child)
    callback.on_chain_end("secret", run_id=parent)
    invocation.finish_execution()
    assert owner.flush(2).pending_records == 0
    events = [event for body in owner.client.writes for event in body.get("events", [])]
    assert len(events) == 2
    tool = next(event for event in events if event["observation_kind"] == "tool_call")
    chain = next(event for event in events if event["observation_kind"] == "span")
    assert tool["parent_event_id"] == chain["id"]
    assert "input_json" not in tool and "output_json" not in tool


def test_generic_graph_is_excluded_and_native_model_only_is_unknown(bind):
    import pytest
    from openeight import ConfigurationError
    from langgraph.graph import StateGraph
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from langchain_core.messages import AIMessage
    from typing import TypedDict

    class State(TypedDict):
        value: str

    graph = StateGraph(State)
    graph.add_node("model", lambda state: state)
    graph.set_entry_point("model")
    graph.set_finish_point("model")
    with pytest.raises(ConfigurationError):
        bind(graph.compile())
    owner = bind(
        create_agent(FakeMessagesListChatModel(responses=[AIMessage(content="done")]))
    )
    assert owner.delivery_status().discovery_completeness == "unknown"
    assert owner.invoke({"messages": [("user", "x")]})["messages"][-1].content == "done"
    assert owner.flush(2).pending_records == 0


def test_child_before_parent_uses_the_parent_placeholder_id(bind):
    owner = bind(fixture()[0])
    invocation = owner._admit_invocation({}, "invoke")
    callback = ObservationCallbacks(invocation)
    parent, child = uuid4(), uuid4()
    callback.on_tool_start(
        {"name": "echo"}, "secret", run_id=child, parent_run_id=parent
    )
    callback.on_tool_end("secret", run_id=child)
    callback.on_chain_start({}, {}, run_id=parent)
    callback.on_chain_end("secret", run_id=parent)
    invocation.finish_execution()
    assert owner.flush(2).pending_records == 0
    events = [event for body in owner.client.writes for event in body.get("events", [])]
    assert len(events) == 2
    tool = next(event for event in events if event["observation_kind"] == "tool_call")
    chain = next(event for event in events if event["observation_kind"] == "span")
    assert tool["parent_event_id"] == chain["id"]


def test_nested_callback_does_not_inherit_another_read_tool_contract(bind):
    from types import SimpleNamespace
    from openeight.langchain._runtime import CURRENT_READ_CONTRACT

    owner = bind(fixture()[0])
    invocation = owner._admit_invocation({}, "invoke")
    inner = SimpleNamespace(tool=SimpleNamespace(name="echo"))
    outer = SimpleNamespace(tool=SimpleNamespace(name="outer"))
    invocation.contracts["echo"] = inner
    callback = ObservationCallbacks(invocation)
    child = uuid4()
    token = CURRENT_READ_CONTRACT.set(outer)
    try:
        callback.on_tool_start({"name": "echo"}, "", run_id=child)
        assert invocation.entries[child]["tool_contract"] is inner
    finally:
        CURRENT_READ_CONTRACT.reset(token)
        invocation.expire_callbacks()
        invocation.finish_execution()


def test_names_roles_and_interrupted_ancestors(bind):
    from langgraph.errors import GraphInterrupt

    owner = bind(fixture()[0])
    invocation = owner._admit_invocation({}, "invoke")
    callbacks = ObservationCallbacks(invocation)
    root, child = uuid4(), uuid4()
    callbacks.on_chain_start({}, {}, run_id=root, name="LangGraph")
    callbacks.on_chain_start(
        {},
        {},
        run_id=child,
        parent_run_id=root,
        name="refund_step",
        metadata={"langgraph_node": "refund_step"},
    )
    callbacks.on_chain_error(GraphInterrupt(), run_id=child)
    callbacks.on_chain_end({}, run_id=root)
    invocation.finish_execution()
    assert owner.flush(2).pending_records == 0
    events = [event for body in owner.client.writes for event in body.get("events", [])]
    assert {e["name"]: (e["span_role"], e["outcome"]) for e in events} == {
        "LangGraph": ("graph", "interrupted"),
        "refund_step": ("node", "interrupted"),
    }
    assert all("error_json" not in e for e in events)


def test_multibyte_name_preserves_lifecycle_suffix():
    from openeight.langchain._callbacks import bounded_name

    result = bounded_name("返金" * 100, suffix=".paused")
    assert len(result.encode()) <= 128 and result.endswith(".paused")


def test_read_telemetry_retains_captured_governance_revision(bind):
    import json
    from types import SimpleNamespace
    from openeight.langchain._runtime import CURRENT_READ_CONTRACT

    owner = bind(fixture()[0])
    invocation = owner._admit_invocation({}, "invoke")
    revision = str(uuid4())
    contract = SimpleNamespace(
        tool=SimpleNamespace(
            name="echo", id=str(uuid4()), governed=True, capability="read"
        ),
        tool_version=SimpleNamespace(id=str(uuid4()), governance_revision_id=revision),
    )
    callback = ObservationCallbacks(invocation)
    child = uuid4()
    token = CURRENT_READ_CONTRACT.set(contract)
    try:
        callback.on_tool_start({"name": "echo"}, "read", run_id=child)
    finally:
        CURRENT_READ_CONTRACT.reset(token)
    invocation.contracts["echo"] = SimpleNamespace(tool=SimpleNamespace(name="echo"))
    callback.on_tool_end("result", run_id=child)
    invocation.finish_execution()
    assert owner.flush(2).pending_records == 0
    events = [event for body in owner.client.writes for event in body.get("events", [])]
    event = next(event for event in events if event["observation_kind"] == "tool_call")
    assert (
        json.loads(event["attributes_json"])["openeight.tool.governance_revision_id"]
        == revision
    )


def test_bound_read_callback_links_pinned_ids_without_registration_records(bind):
    import json
    import pytest
    from openeight.langchain._runtime import CURRENT_READ_CONTRACT

    pytest.importorskip("deepagents")
    from openeight.deepagents._deployment import BoundExecutionContract

    owner = bind(fixture()[0])
    invocation = owner._admit_invocation({}, "invoke")
    invocation.durable = True
    owner._integration = "deepagents"
    revision = str(uuid4())
    from types import SimpleNamespace

    contract = BoundExecutionContract(
        (),
        "echo",
        "echo",
        str(uuid4()),
        str(uuid4()),
        "{}",
        "read",
        SimpleNamespace(governance_revision_id=revision),
    )
    invocation.contracts["echo"] = contract
    callback = ObservationCallbacks(invocation)
    child = uuid4()
    token = CURRENT_READ_CONTRACT.set(contract)
    try:
        callback.on_tool_start({"name": "echo"}, "read", run_id=child)
    finally:
        CURRENT_READ_CONTRACT.reset(token)
    callback.on_tool_end("result", run_id=child)
    invocation.finish_execution()
    assert owner.flush(2).pending_records == 0
    events = [event for body in owner.client.writes for event in body.get("events", [])]
    event = next(event for event in events if event["observation_kind"] == "tool_call")
    assert event["tool_id"] == contract.tool_id
    assert event["tool_version_id"] == contract.tool_version_id
    assert (
        json.loads(event["attributes_json"])["openeight.tool.governance_revision_id"]
        == revision
    )
    assert not getattr(invocation, "persistence_failed", False)
