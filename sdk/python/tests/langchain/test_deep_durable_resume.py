"""Native interrupt correlation uses the persisted parent and exact child binding."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

pytest.importorskip("deepagents")

from langgraph.types import Interrupt
from test_durable_native_results import deep_fixture

from openeight import ConfigurationError
from openeight.deepagents._continuation import DurableCallContext, durable_attempt_key
from openeight.deepagents._frameworks._checkpoints import (
    DeepSnapshot,
    NativeTaskProjection,
)
from openeight.deepagents._frameworks._identity import NativeObservations
from openeight.deepagents._frameworks._resume import resume_input
from openeight.deepagents.durable import DurableContinuation
from openeight.telemetry import uuid7


def fixture():
    attempt, invocation, continuation, _ = deep_fixture(("first",))
    root = continuation.checkpoint_namespace.split("|")[0]
    parent_call = {
        "name": "task",
        "id": "parent-native",
        "args": {"description": "work", "subagent_type": "first"},
    }
    parent_key = durable_attempt_key(
        DurableCallContext((), root, None, "delegation"), "parent-native"
    )
    parent_continuation = DurableContinuation(
        "deployment",
        "thread",
        "parent-decision",
        "[]",
        root,
        "parent-native",
        parent_key,
        "delegation",
    )
    parent = SimpleNamespace(
        deployment_id="deployment",
        thread_id="thread",
        decision_id="parent-decision",
        tool_call_id=parent_key,
        status="executing",
        event_id=uuid7(),
        native_call_json=json.dumps(parent_call),
    )
    continuation = replace(continuation, parent_attempt_key=parent_key)
    records = {parent_key: (parent, parent_continuation)}
    invocation.store.attempts = lambda _: tuple(a for a, _ in records.values())
    invocation.store.continuation = lambda thread, key: records.get(key, (None, None))[
        1
    ]
    invocation.store.get_attempt = lambda thread, key: records.get(key, (None, None))[0]
    invocation.owner._composition = {
        "topology": [
            {"path": [], "delegates_to": [["first"], ["second"]]},
            {"path": ["first"], "delegates_to": []},
            {"path": ["second"], "delegates_to": []},
        ]
    }
    from uuid import uuid4

    from openeight.deepagents._continuation import public_native_call
    from openeight.deepagents._deployment import BoundCatalogSnapshot
    from openeight.deepagents._durable_calls import prepare
    from openeight.tools import PolicyInputRequirement, ToolReadiness

    bound = next(
        c
        for c in invocation.owner._bound_catalog.contracts
        if c.child_path == ("first",)
    )
    ready = ToolReadiness(
        None,
        bound.catalog_name,
        bound.tool_id,
        bound.tool_version_id,
        "registered",
        True,
        True,
        True,
        "ready",
        "a" * 64,
        (
            PolicyInputRequirement(
                str(uuid4()), str(uuid4()), str(uuid4()), "b" * 64, ()
            ),
        ),
    )
    bound = replace(bound, readiness=ready)
    invocation.owner._bound_catalog = BoundCatalogSnapshot(
        tuple(
            bound if c.child_path == ("first",) else c
            for c in invocation.owner._bound_catalog.contracts
        )
    )
    invocation.owner.control = SimpleNamespace(
        durable=SimpleNamespace(max_tool_attempts=64)
    )
    invocation.recovery_namespaces = frozenset()
    invocation.run_id = str(uuid4())
    invocation.store.load = lambda _: SimpleNamespace(read_calls=0)

    def persist(attempt, continuation):
        records[attempt.tool_call_id] = (attempt, continuation)
        return attempt

    invocation.store.prepare_continuation = persist
    public_call = public_native_call(attempt, continuation)
    binding = invocation.owner.catalog.resolve(("first",), "refund")
    prepared, _, _ = prepare(
        invocation,
        SimpleNamespace(tool=binding.tool, tool_call=public_call),
        bound,
        call_context=DurableCallContext(
            ("first",), continuation.checkpoint_namespace, parent_key, "business_write"
        ),
    )
    attempt = replace(prepared, status="waiting")
    continuation = records[attempt.tool_call_id][1]
    records[attempt.tool_call_id] = (attempt, continuation)

    observations = NativeObservations(
        paths=[(), ("first",), ("second",)], thread_id="thread"
    )

    def config(namespace, parents):
        return {
            "configurable": {
                "thread_id": "thread",
                "checkpoint_ns": namespace,
                "__pregel_task_id": namespace.rsplit(":", 1)[1],
                "checkpoint_map": parents,
            },
            "metadata": {
                "langgraph_checkpoint_ns": namespace,
                "langgraph_node": "tools",
            },
        }

    observations.observe(config(root, {"": "root-input"}), parent_call, child_path=())
    child_call = public_native_call(attempt, continuation)
    observations.observe(
        config(
            continuation.checkpoint_namespace, {"": "root-input", root: "child-input"}
        ),
        child_call,
        child_path=("first",),
    )
    invocation.native_observations = observations
    interrupt = Interrupt(
        value={"decision_id": attempt.decision_id}, id="native-interrupt"
    )
    tasks = (
        NativeTaskProjection(
            root,
            (),
            "root-input",
            parent_call,
            SimpleNamespace(
                id=root.removeprefix("tools:"), error=None, interrupts=(interrupt,)
            ),
            (),
        ),
        NativeTaskProjection(
            continuation.checkpoint_namespace,
            ("first",),
            "child-input",
            child_call,
            SimpleNamespace(
                id=continuation.checkpoint_namespace.rsplit(":", 1)[1],
                error=None,
                interrupts=(interrupt,),
            ),
            (),
        ),
    )
    snapshot = SimpleNamespace(native=DeepSnapshot(None, tasks, observations))
    return invocation, snapshot, records, attempt, continuation


def test_nested_interrupt_correlates_to_child_and_deduplicates_native_parent_propagation():
    invocation, snapshot, _, attempt, _ = fixture()
    result = resume_input(snapshot, invocation=invocation)
    assert result.resume == {"native-interrupt": {"decision_id": attempt.decision_id}}
    assert (
        json.loads(attempt.native_call_json)["args"]["idempotency_key"]
        == attempt.idempotency_key
    )
    assert "idempotency_key" not in snapshot.native.tasks[1].call["args"]


@pytest.mark.parametrize(
    "status", ["executing", "reconciliation_required", "succeeded", "failed", "blocked"]
)
def test_historical_interrupt_does_not_resume_an_already_advanced_attempt(status):
    invocation, snapshot, records, attempt, continuation = fixture()
    records[attempt.tool_call_id] = (replace(attempt, status=status), continuation)
    assert resume_input(snapshot, invocation=invocation) is None
    from openeight.deepagents._frameworks._resume import waiting_task

    with pytest.raises(ValueError):
        waiting_task(
            snapshot, invocation=invocation, attempt=records[attempt.tool_call_id][0]
        )


def test_prepared_attempt_cannot_claim_a_historical_interrupt():
    invocation, snapshot, records, attempt, continuation = fixture()
    records[attempt.tool_call_id] = (replace(attempt, status="prepared"), continuation)
    with pytest.raises(ConfigurationError) as caught:
        resume_input(snapshot, invocation=invocation)
    assert caught.value.reason == "invalid_resume_correlation"


@pytest.mark.parametrize(
    "change",
    [
        "wrong_scope",
        "wrong_namespace",
        "settled_parent",
        "missing_child",
        "wrong_path",
        "forged_interrupt",
        "duplicate_decision",
    ],
)
def test_invalid_saved_child_or_parent_never_builds_resume_input(change):
    invocation, snapshot, records, attempt, continuation = fixture()
    if change == "wrong_scope":
        records[attempt.tool_call_id] = (
            replace(attempt, thread_id="foreign"),
            continuation,
        )
    elif change == "wrong_namespace":
        records[attempt.tool_call_id] = (
            attempt,
            replace(
                continuation,
                checkpoint_namespace="tools:33333333-3333-4333-8333-333333333333",
            ),
        )
    elif change == "settled_parent":
        records[continuation.parent_attempt_key][0].status = "succeeded"
    elif change == "missing_child":
        snapshot.native = replace(snapshot.native, tasks=snapshot.native.tasks[:1])
    elif change == "wrong_path":
        records[attempt.tool_call_id] = (
            attempt,
            replace(continuation, child_path_json='["second"]'),
        )
    elif change == "duplicate_decision":
        existing = invocation.store.attempts(invocation.thread_id)
        invocation.store.attempts = lambda _: existing + (attempt,)
    else:
        snapshot.native.tasks[1].task.interrupts = (
            Interrupt(value={"decision_id": "foreign-decision"}, id="native-interrupt"),
        )
    with pytest.raises(ConfigurationError) as caught:
        resume_input(snapshot, invocation=invocation)
    assert caught.value.reason == "invalid_resume_correlation"


def test_nonancestor_sibling_cannot_propagate_another_childs_interrupt():
    invocation, snapshot, _, _, _ = fixture()
    root = "tools:33333333-3333-4333-8333-333333333333"
    call = {
        "name": "task",
        "id": "other-parent",
        "args": {"description": "other", "subagent_type": "second"},
    }
    invocation.native_observations.observe(
        {
            "configurable": {
                "thread_id": "thread",
                "checkpoint_ns": root,
                "__pregel_task_id": root.removeprefix("tools:"),
                "checkpoint_map": {"": "root-input"},
            },
            "metadata": {"langgraph_checkpoint_ns": root, "langgraph_node": "tools"},
        },
        call,
        child_path=(),
    )
    sibling = NativeTaskProjection(
        root,
        (),
        "root-input",
        call,
        SimpleNamespace(interrupts=snapshot.native.tasks[1].task.interrupts),
        (),
    )
    snapshot.native = replace(snapshot.native, tasks=snapshot.native.tasks + (sibling,))
    with pytest.raises(ConfigurationError) as caught:
        resume_input(snapshot, invocation=invocation)
    assert caught.value.reason == "invalid_resume_correlation"


def test_changed_sdk_key_cannot_be_stripped_to_make_a_business_call_match():
    from uuid import uuid4

    invocation, snapshot, records, attempt, continuation = fixture()
    altered = replace(attempt, idempotency_key=str(uuid4()))
    records[attempt.tool_call_id] = (altered, continuation)
    with pytest.raises(ConfigurationError) as caught:
        resume_input(snapshot, invocation=invocation)
    assert caught.value.reason == "invalid_resume_correlation"


def test_executing_parent_is_a_valid_barrier_only_for_its_pending_child():
    from openeight.deepagents._frameworks._pending import pending_delegations

    invocation, snapshot, _, _, continuation = fixture()
    assert pending_delegations(snapshot, invocation=invocation) == frozenset(
        {continuation.parent_attempt_key}
    )
    snapshot.native.tasks[0].task.error = "cancelled"
    with pytest.raises(ConfigurationError) as caught:
        pending_delegations(snapshot, invocation=invocation)
    assert caught.value.reason == "durable_reconciliation_required"
