"""Path-bound immutable preparation; these are not server governance tests."""

import copy
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("deepagents")

from deepagents.middleware.filesystem import FilesystemMiddleware

from openeight import ConfigurationError
from openeight.deepagents._continuation import DurableCallContext
from openeight.deepagents._deployment import (
    BoundCatalogSnapshot,
    BoundExecutionContract,
    canonical,
)
from openeight.deepagents._durable_calls import prepare, validate_call
from openeight.deepagents._frameworks._generated import CapturedStateBackend
from openeight.deepagents._frameworks.v1 import DeepAgentsV1
from openeight.deepagents._local_durable._store import SQLiteDeepAgentsStore
from openeight.langchain._bindings import AdapterBindings
from openeight.tools import PolicyInputRequirement, ToolReadiness


@pytest.fixture
def prepared_context(tmp_path):
    from test_local_durable_store import IDENTITY, manifest, measured_manifest

    backend = CapturedStateBackend()
    tool = next(
        t for t in FilesystemMiddleware(backend=backend).tools if t.name == "write_file"
    )
    bindings = AdapterBindings({(): (tool,)}, deep=True)
    binding = bindings.resolve((), "write_file")
    tool_id, version_id = str(uuid4()), str(uuid4())
    requirement = PolicyInputRequirement(
        str(uuid4()), str(uuid4()), str(uuid4()), "b" * 64, ()
    )
    ready = ToolReadiness(
        None,
        binding.facts.name,
        tool_id,
        version_id,
        "registered",
        True,
        True,
        True,
        "ready",
        "a" * 64,
        (requirement,),
    )
    contract = BoundExecutionContract(
        (),
        "write_file",
        binding.facts.name,
        tool_id,
        version_id,
        binding.schema_bytes.decode(),
        "state_write",
        ready,
    )
    store = SQLiteDeepAgentsStore(tmp_path)
    compatibility = DeepAgentsV1()
    compatibility._backend = backend
    compatibility._graphs = {(): None}
    owner = SimpleNamespace(
        catalog=bindings,
        _bound_catalog=BoundCatalogSnapshot((contract,)),
        _compatibility=compatibility,
        control=SimpleNamespace(durable=SimpleNamespace(max_tool_attempts=64)),
    )
    with store.acquire_deployment():
        deployment = store.initialize_enrollment(canonical(IDENTITY), "a" * 64)
        document = json.loads(manifest(deployment))
        document["topology"][0]["tools"] = [
            {
                "native_name": "write_file",
                "catalog_name": binding.facts.name,
                "tool_id": tool_id,
                "tool_version_id": version_id,
                "input_schema_sha256": hashlib.sha256(binding.schema_bytes).hexdigest(),
                "recovery_kind": "state_write",
            }
        ]
        store.bind_deployment(deployment.deployment_id, measured_manifest(document))
        admission = store.admit(kind="start", command_id="command", message="go")
        with store.acquire(admission.thread_id):
            command, thread = (
                store.command(admission.thread_id, "command"),
                store.load(admission.thread_id),
            )
            store.begin_command(
                thread.id, command.id, command.revision, thread.revision
            )
            invocation = SimpleNamespace(
                owner=owner,
                store=store,
                deployment_id=deployment.deployment_id,
                thread_id=thread.id,
                run_id=thread.run_id,
                recovering=False,
                recovery_namespaces=frozenset(),
            )
            request = SimpleNamespace(
                tool=tool,
                tool_call={
                    "id": "same-call",
                    "name": "write_file",
                    "args": {"file_path": "/report", "content": "hello"},
                    "type": "tool_call",
                },
            )
            context = DurableCallContext(
                (), "tools:" + str(uuid4()), None, "state_write"
            )
            yield invocation, request, context, contract


def test_preparation_freezes_path_key_contract_and_native_call_atomically(
    prepared_context,
):
    invocation, request, context, contract = prepared_context
    attempt, effective, result_contract = prepare(
        invocation, request, contract, call_context=context
    )
    continuation = invocation.store.continuation(
        invocation.thread_id, attempt.tool_call_id
    )
    assert attempt.tool_call_id != request.tool_call["id"]
    assert attempt.tool_name == contract.catalog_name
    assert continuation.native_tool_call_id == request.tool_call["id"]
    assert effective == request.tool_call
    assert attempt.idempotency_key is None and result_contract == contract
    assert prepare(invocation, request, None, call_context=context)[0] == attempt
    other_context = replace(context, checkpoint_namespace="tools:" + str(uuid4()))
    other, _, _ = prepare(invocation, request, contract, call_context=other_context)
    assert (
        other.decision_id != attempt.decision_id
        and other.tool_call_id != attempt.tool_call_id
    )


def test_preparation_does_not_accept_changed_duplicate_input(prepared_context):
    invocation, request, context, contract = prepared_context
    prepare(invocation, request, contract, call_context=context)
    changed = copy.deepcopy(request.tool_call)
    changed["args"]["content"] = "different"
    with pytest.raises(ConfigurationError) as caught:
        prepare(
            invocation,
            SimpleNamespace(tool=request.tool, tool_call=changed),
            contract,
            call_context=context,
        )
    assert caught.value.reason == "durable_attempt_conflict"
    assert len(invocation.store.attempts(invocation.thread_id)) == 1


@pytest.mark.parametrize(
    "args",
    [
        {"file_path": "/file", "content": "x", "extra": True},
        {"file_path": "/file", "content": "x", "idempotency_key": "model-owned"},
        {"file_path": "/file"},
        {"file_path": "/file", "content": 12},
    ],
)
def test_invalid_native_arguments_never_create_a_durable_attempt(
    prepared_context, args
):
    invocation, request, context, contract = prepared_context
    request.tool_call = {**request.tool_call, "args": args}
    with pytest.raises(ConfigurationError) as caught:
        prepare(invocation, request, contract, call_context=context)
    assert caught.value.reason == "invalid_input"
    assert invocation.store.attempts(invocation.thread_id) == ()


def test_other_path_or_recovery_kind_cannot_select_a_contract(prepared_context):
    invocation, request, context, _ = prepared_context
    for changed in (
        replace(context, child_path=("foreign",)),
        replace(context, kind="business_write"),
    ):
        with pytest.raises(ConfigurationError):
            validate_call(invocation, request, call_context=changed)
    assert invocation.store.attempts(invocation.thread_id) == ()


@pytest.mark.parametrize(
    "change",
    [
        {
            "governed": False,
            "policy_contract_set_digest": None,
            "policy_input_contracts": (),
        },
        {
            "eligible": False,
            "policy_contract_set_digest": None,
            "policy_input_contracts": (),
        },
        {
            "policy_ready": False,
            "policy_contract_set_digest": None,
            "policy_input_contracts": (),
        },
        {"tool_id": str(uuid4())},
        {"tool_version_id": str(uuid4())},
        {"name": "different"},
        {"policy_input_contracts": ()},
        {"policy_contract_set_digest": "c" * 64},
        {"policy_contract_set_digest": None, "policy_input_contracts": ()},
    ],
)
def test_duplicate_attempt_still_requires_current_exact_write_readiness(
    prepared_context, change
):
    invocation, request, context, contract = prepared_context
    original, _, _ = prepare(invocation, request, contract, call_context=context)
    changed = replace(contract, readiness=replace(contract.readiness, **change))
    invocation.owner._bound_catalog = BoundCatalogSnapshot((changed,))
    with pytest.raises(ConfigurationError) as caught:
        prepare(invocation, request, None, call_context=context)
    assert caught.value.reason in {"governance_not_ready", "governance_unconfigured"}
    assert invocation.store.attempts(invocation.thread_id) == (original,)


@pytest.mark.parametrize("status", ["succeeded", "failed", "blocked"])
def test_terminal_generated_attempt_replays_after_policy_only_change(
    prepared_context, status
):
    from openeight.deepagents.durable import GeneratedStateReceipt
    from openeight.langchain.durable import (
        AttemptSettlement,
        AttemptTransition,
        DurableObserveRecord,
    )

    invocation, request, context, contract = prepared_context
    store = invocation.store
    attempt, _, _ = prepare(invocation, request, contract, call_context=context)
    result = canonical(
        dict(content="captured", status="success" if status == "succeeded" else "error")
    )
    receipt = None
    if status == "blocked":
        settled = store.settle_attempt(
            invocation.thread_id,
            attempt.decision_id,
            attempt.revision,
            AttemptSettlement(status, result, "deny", None),
        )
    else:
        attempt = store.transition_attempt(
            invocation.thread_id,
            attempt.decision_id,
            attempt.revision,
            AttemptTransition("executing"),
        )
        event = json.loads(attempt.event_template_json)
        event["outcome"] = "success" if status == "succeeded" else "error"
        if status == "failed":
            event["error_json"] = canonical(dict(kind="tool_error"))
        record = DurableObserveRecord(
            attempt.event_id,
            invocation.thread_id,
            "event",
            canonical(event),
            attempt.created_at,
        )
        receipt = GeneratedStateReceipt(
            invocation.deployment_id,
            invocation.thread_id,
            attempt.decision_id,
            1,
            str(uuid4()),
            "{}",
        )
        settled = store.settle_generated_attempt(
            invocation.thread_id,
            attempt.tool_call_id,
            attempt.revision,
            AttemptSettlement(status, result, None, record),
            receipt,
        )
    outbox = store.pending(invocation.thread_id)
    changed = replace(
        contract,
        readiness=replace(contract.readiness, policy_contract_set_digest="c" * 64),
    )
    invocation.owner._bound_catalog = BoundCatalogSnapshot((changed,))
    replayed, _, _ = prepare(invocation, request, None, call_context=context)
    assert replayed == settled
    assert (
        store.generated_state_receipt(invocation.thread_id, attempt.tool_call_id)
        == receipt
    )
    assert store.pending(invocation.thread_id) == outbox


def test_recovered_native_task_cannot_gain_new_authority_after_sibling_model_progress(
    prepared_context,
):
    invocation, request, context, contract = prepared_context
    invocation.recovery_namespaces = frozenset({context.checkpoint_namespace})
    # Another branch's model progress cannot erase the resumed task's identity.
    invocation.recovering = False
    with pytest.raises(ConfigurationError) as caught:
        prepare(invocation, request, contract, call_context=context)
    assert caught.value.reason == "durable_attempt_missing"
    assert invocation.store.attempts(invocation.thread_id) == ()


def test_call_context_comes_from_observed_native_identity_and_bound_recovery_kind(
    prepared_context,
):
    from openeight.deepagents._continuation import capture_durable_call_context
    from openeight.deepagents._frameworks._identity import NativeObservations

    invocation, request, context, _ = prepared_context
    invocation.native_observations = NativeObservations(
        paths=[()], thread_id=invocation.thread_id
    )
    config = {
        "configurable": {
            "thread_id": invocation.thread_id,
            "checkpoint_ns": context.checkpoint_namespace,
            "__pregel_task_id": context.checkpoint_namespace.removeprefix("tools:"),
            "checkpoint_map": {"": str(uuid4())},
        },
        "metadata": {
            "langgraph_checkpoint_ns": context.checkpoint_namespace,
            "langgraph_node": "tools",
        },
    }
    native = invocation.native_observations.observe(
        config, request.tool_call, child_path=()
    )
    assert (
        capture_durable_call_context(
            request, invocation=invocation, native=native, parent_attempt_key=None
        )
        == context
    )
    with pytest.raises(ConfigurationError) as caught:
        capture_durable_call_context(
            request,
            invocation=invocation,
            native=replace(native, checkpoint_namespace="tools:" + str(uuid4())),
            parent_attempt_key=None,
        )
    assert caught.value.reason == "invalid_resume_correlation"
    with pytest.raises(ConfigurationError) as caught:
        capture_durable_call_context(
            request,
            invocation=invocation,
            native=native,
            parent_attempt_key="invented-parent",
        )
    assert caught.value.reason == "invalid_resume_correlation"


@pytest.mark.asyncio
async def test_concurrent_model_budget_reservations_read_revision_after_ownership():
    import asyncio

    from openeight.langchain._authorization import OpenEightMiddleware
    from openeight.langchain.durable import DurableApprovalConfig

    thread = SimpleNamespace(id="thread", revision=0, model_calls=0)
    transitions, effects = [], []

    def load(_):
        return copy.copy(thread)

    def transition(thread_id, expected_revision, update):
        assert expected_revision == thread.revision
        thread.revision += 1
        thread.model_calls += update.model_call_increment
        transitions.append(thread.model_calls)

    store = SimpleNamespace(load=load, transition=transition)

    async def reconcile(_):
        raise AssertionError("not an effect recovery")

    async def classify(_):
        return "terminal"

    control = OpenEightMiddleware(
        agent_name="test",
        endpoint="http://localhost:7777",
        credential="ctk_test",
        durable=DurableApprovalConfig(
            store=store, reconcile=reconcile, classify_turn=classify, max_model_calls=1
        ),
    )
    lock, queued = asyncio.Lock(), asyncio.Event()
    count = 0
    await lock.acquire()

    async def storage_call(function):
        nonlocal count
        count += 1
        if count == 2:
            queued.set()
        async with lock:
            return function()

    import threading

    invocation = SimpleNamespace(
        thread_id="thread", storage_call=storage_call, lock=threading.RLock()
    )
    control.owner = SimpleNamespace(lock=threading.RLock(), _ensure_open=lambda: None)
    control._require_invocation = lambda: invocation

    async def handler(_):
        effects.append("model")
        return "completed"

    tasks = [
        asyncio.create_task(control.awrap_model_call(None, handler)) for _ in range(2)
    ]
    await asyncio.wait_for(queued.wait(), 2)
    lock.release()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert results[0] == "completed"
    assert (
        isinstance(results[1], ConfigurationError)
        and results[1].reason == "durable_limit_exhausted"
    )
    assert effects == ["model"] and transitions == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["success", "error"])
@pytest.mark.parametrize("fail", [False, True])
async def test_shared_generated_settlement_is_atomic_with_receipt_result_and_event(
    prepared_context, status, fail
):
    from openeight._framework._types import CapturedGeneratedResult, NativeToolResult
    from openeight._time import utc_timestamp
    from openeight.deepagents.durable import GeneratedStateReceipt
    from openeight.langchain._durable_runtime import DurableInvocation
    from openeight.langchain.durable import AttemptTransition

    invocation, request, context, contract = prepared_context
    attempt, _, _ = prepare(invocation, request, contract, call_context=context)
    attempt = invocation.store.transition_attempt(
        invocation.thread_id,
        attempt.decision_id,
        attempt.revision,
        AttemptTransition("executing"),
    )
    invocation.owner._integration = "deepagents"
    invocation.continuation = lambda saved: DurableInvocation.continuation(
        invocation, saved
    )
    invocation.capture_content = lambda *args: None

    async def storage_call(function):
        return function()

    invocation.storage_call = storage_call
    result = NativeToolResult(
        request.tool_call["id"], request.tool_call["name"], "captured", status
    )
    receipt = GeneratedStateReceipt(
        invocation.deployment_id,
        invocation.thread_id,
        attempt.decision_id,
        1,
        str(uuid4()),
        "{}",
    )
    captured = CapturedGeneratedResult(result, receipt, object())
    message = invocation.owner._compatibility.tool_message(result)
    if fail:

        def fault(point):
            if point == "after_generated_receipt_insert":
                raise RuntimeError("simulated termination boundary")

        invocation.store.fault = fault
        with pytest.raises(RuntimeError):
            await DurableInvocation.settle(
                invocation,
                attempt,
                message,
                generated=captured,
                execution_started_at=utc_timestamp(),
                execution_latency_ms=0,
            )
        assert (
            invocation.store.generated_state_receipt(
                invocation.thread_id, attempt.tool_call_id
            )
            is None
        )
        assert (
            invocation.store.get_attempt(
                invocation.thread_id, attempt.tool_call_id
            ).status
            == "executing"
        )
        assert all(
            record.id != attempt.event_id
            for record in invocation.store.pending(invocation.thread_id)
        )
    else:
        settled = await DurableInvocation.settle(
            invocation,
            attempt,
            message,
            generated=captured,
            execution_started_at=utc_timestamp(),
            execution_latency_ms=0,
        )
        assert settled.status == ("succeeded" if status == "success" else "failed")
        assert (
            invocation.store.generated_state_receipt(
                invocation.thread_id, attempt.tool_call_id
            )
            == receipt
        )
        records = [
            r
            for r in invocation.store.pending(invocation.thread_id)
            if r.id == attempt.event_id
        ]
        assert (
            len(records) == 1
            and json.loads(records[0].payload_json)["outcome"] == status
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("native_state", ["pending", "complete", "partial_error"])
@pytest.mark.parametrize("proof", ["known", "unknown"])
async def test_generated_recovery_requires_saver_proof_before_any_journal_settlement(
    prepared_context, monkeypatch, native_state, proof
):
    from langgraph._internal._constants import ERROR

    from openeight._framework._types import GeneratedReplayState, NativeToolResult
    from openeight.deepagents._frameworks._checkpoints import NativeTaskProjection
    from openeight.deepagents._frameworks._recovery import recover_generated
    from openeight.langchain._durable_runtime import DurableInvocation
    from openeight.langchain.durable import AttemptTransition

    invocation, request, context, contract = prepared_context
    attempt, _, _ = prepare(invocation, request, contract, call_context=context)
    attempt = invocation.store.transition_attempt(
        invocation.thread_id,
        attempt.decision_id,
        attempt.revision,
        AttemptTransition("executing"),
    )
    continuation = invocation.store.continuation(
        invocation.thread_id, attempt.tool_call_id
    )
    invocation.owner._integration = "deepagents"
    invocation.continuation = lambda saved: DurableInvocation.continuation(
        invocation, saved
    )
    invocation.capture_content = lambda *args: None
    invocation.generated_replays = {}

    async def storage_call(function):
        return function()

    invocation.storage_call = storage_call

    async def settle(*args, **kwargs):
        return await DurableInvocation.settle(invocation, *args, **kwargs)

    invocation.settle = settle
    compatibility = invocation.owner._compatibility
    monkeypatch.setattr(
        compatibility, "tool_output_routes", lambda _: frozenset({"branch:to:model"})
    )
    identifier = context.checkpoint_namespace.removeprefix("tools:")
    message = compatibility.tool_message(
        NativeToolResult(
            request.tool_call["id"], request.tool_call["name"], "saved", "success"
        )
    )
    writes = (
        []
        if native_state == "pending"
        else [
            (identifier, "messages", [message]),
            (identifier, "branch:to:model", None),
        ]
    )
    if native_state == "partial_error":
        writes.append((identifier, ERROR, "cancelled"))
    task = NativeTaskProjection(
        context.checkpoint_namespace,
        (),
        str(uuid4()),
        request.tool_call,
        SimpleNamespace(
            id=identifier,
            error="cancelled" if native_state == "partial_error" else None,
        ),
        tuple(writes),
    )
    snapshot = SimpleNamespace(
        native=SimpleNamespace(
            tasks=(task,),
            observations=SimpleNamespace(
                observations={
                    task.namespace: SimpleNamespace(
                        native_call_id=request.tool_call["id"],
                        identity=SimpleNamespace(child_path=()),
                    ),
                }
            ),
        )
    )
    checks = []

    async def inspect(snapshot_arg, **kwargs):
        checks.append(kwargs)
        # No immutable result/receipt may be stored before this proof returns.
        assert (
            invocation.store.get_attempt(
                invocation.thread_id, attempt.tool_call_id
            ).status
            == "executing"
        )
        assert (
            invocation.store.generated_state_receipt(
                invocation.thread_id, attempt.tool_call_id
            )
            is None
        )
        if proof == "unknown":
            return GeneratedReplayState("unknown", None)
        return GeneratedReplayState(
            "replayable" if native_state == "pending" else "already_committed", task
        )

    monkeypatch.setattr(compatibility, "inspect_generated_replay", inspect)
    result = await recover_generated(
        compatibility, invocation, attempt, continuation, snapshot
    )
    if native_state == "partial_error" or proof == "unknown":
        assert result == "reconciliation_required"
        assert (
            invocation.store.get_attempt(
                invocation.thread_id, attempt.tool_call_id
            ).status
            == "executing"
        )
        assert (
            invocation.store.generated_state_receipt(
                invocation.thread_id, attempt.tool_call_id
            )
            is None
        )
    elif native_state == "pending":
        assert result == "resume_required" and len(checks) == 1
        assert (
            invocation.store.generated_state_receipt(
                invocation.thread_id, attempt.tool_call_id
            )
            is None
        )
    else:
        assert result == "restored" and len(checks) == 1
        assert (
            invocation.store.get_attempt(
                invocation.thread_id, attempt.tool_call_id
            ).status
            == "succeeded"
        )
        assert (
            invocation.store.generated_state_receipt(
                invocation.thread_id, attempt.tool_call_id
            ).input_checkpoint_id
            == task.input_checkpoint_id
        )


@pytest.mark.asyncio
async def test_native_seal_while_write_reservation_waits_prevents_effect_claim(
    monkeypatch,
):
    import asyncio
    import threading

    from langgraph.prebuilt.tool_node import ToolCallRequest
    from test_deep_durable_resume import fixture

    from openeight.deepagents import _admission
    from openeight.deepagents._continuation import public_native_call
    from openeight.langchain._durable_runtime import DurableInvocation

    original, _, records, attempt, continuation = fixture()
    invocation = object.__new__(DurableInvocation)
    invocation.__dict__.update(vars(original))
    invocation.lock = threading.RLock()
    invocation.owner.lock = threading.RLock()
    invocation.owner._ensure_open = lambda: None
    invocation.owner._integration = "deepagents"
    invocation.native_cancelled = False
    invocation.handlers = 0
    invocation.store.load = lambda _: SimpleNamespace(
        deployment_id=invocation.deployment_id, read_calls=0
    )

    async def admission(*args, **kwargs):
        return invocation.owner._bound_catalog

    monkeypatch.setattr(_admission, "_validate_durable_admission", admission)

    async def authorize(_):
        return None

    invocation.authorize = authorize
    invocation.lifecycle_record = lambda *args: None

    def transition(thread, decision, revision, update):
        changed = replace(
            records[attempt.tool_call_id][0],
            status=update.status,
            revision=revision + 1,
        )
        records[attempt.tool_call_id] = (changed, continuation)
        return changed

    invocation.store.transition_attempt = transition
    queued, released = asyncio.Event(), asyncio.Event()

    async def storage_call(function):
        if function.__name__ == "reserve_execution":
            queued.set()
            await released.wait()
        return function()

    invocation.storage_call = storage_call
    binding = invocation.owner.catalog.resolve(("first",), "refund")
    request = ToolCallRequest(
        public_native_call(attempt, continuation), binding.tool, {}, None
    )
    calls = []

    async def handler(_):
        calls.append("effect")
        return None

    context = DurableCallContext(
        ("first",),
        continuation.checkpoint_namespace,
        continuation.parent_attempt_key,
        "business_write",
    )
    task = asyncio.create_task(
        invocation._tool_call_serial(request, handler, call_context=context)
    )
    await asyncio.wait_for(queued.wait(), 2)
    invocation.native_cancelled = True
    released.set()
    with pytest.raises(ConfigurationError) as caught:
        await task
    assert caught.value.reason == "durable_reconciliation_required"
    assert calls == [] and invocation.handlers == 0
    assert records[attempt.tool_call_id][0].status == "waiting"


@pytest.mark.asyncio
async def test_native_seal_while_model_reservation_waits_prevents_provider_dispatch():
    import asyncio
    import threading

    from openeight.langchain._authorization import OpenEightMiddleware
    from openeight.langchain.durable import DurableApprovalConfig

    queued, released = asyncio.Event(), asyncio.Event()
    transitions, calls = [], []
    store = SimpleNamespace(
        load=lambda _: SimpleNamespace(id="thread", revision=0, model_calls=0),
        transition=lambda *args: transitions.append("reserved"),
    )

    async def reconcile(_):
        raise AssertionError("no effect recovery")

    async def classify(_):
        return "terminal"

    control = OpenEightMiddleware(
        agent_name="test",
        endpoint="http://localhost:7777",
        credential="ctk_test",
        durable=DurableApprovalConfig(
            store=store, reconcile=reconcile, classify_turn=classify
        ),
    )

    async def storage_call(function):
        queued.set()
        await released.wait()
        return function()

    invocation = SimpleNamespace(
        thread_id="thread",
        native_cancelled=False,
        storage_call=storage_call,
        lock=threading.RLock(),
    )
    control.owner = SimpleNamespace(lock=threading.RLock(), _ensure_open=lambda: None)
    control._require_invocation = lambda: invocation

    async def handler(_):
        calls.append("provider")

    task = asyncio.create_task(control.awrap_model_call(None, handler))
    await asyncio.wait_for(queued.wait(), 2)
    invocation.native_cancelled = True
    released.set()
    with pytest.raises(ConfigurationError) as caught:
        await task
    assert caught.value.reason == "durable_reconciliation_required"
    assert calls == [] and transitions == []
