"""Persisted results retain native correlation independently of catalog IDs."""

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from openeight import ConfigurationError
from openeight.langchain._durable_runtime import (
    freeze_message,
    result_message,
    tool_message,
)
from openeight.langchain._frameworks.v1 import LangChainV1


def fixture():
    attempt = SimpleNamespace(
        native_call_json='{"id":"native-call","name":"refund","args":{}}',
        tool_call_id="native-call",
        tool_name="refund",
        thread_id="thread",
        deployment_id="deployment",
        result_json='{"content":"receipt","status":"success"}',
    )
    invocation = SimpleNamespace(
        thread_id="thread",
        store=SimpleNamespace(
            load=lambda _: SimpleNamespace(deployment_id="deployment")
        ),
        owner=SimpleNamespace(_compatibility=LangChainV1()),
    )
    return attempt, invocation


def test_receipt_replay_and_failure_keep_original_native_call_id_and_name():
    attempt, invocation = fixture()
    replay = tool_message(attempt, invocation=invocation)
    assert (replay.tool_call_id, replay.name, replay.content) == (
        "native-call",
        "refund",
        "receipt",
    )
    denied = result_message(
        attempt, invocation=invocation, content="blocked", status="error"
    )
    assert (denied.tool_call_id, denied.name, denied.status) == (
        "native-call",
        "refund",
        "error",
    )
    assert json.loads(
        freeze_message(replay, invocation=invocation, attempt=attempt)
    ) == {"content": "receipt", "status": "success"}


@pytest.mark.parametrize(
    "change", [{"tool_call_id": "journal-key"}, {"name": "catalog-qualified"}]
)
def test_handler_result_with_wrong_native_identity_is_never_rewritten(change):
    attempt, invocation = fixture()
    result = ToolMessage(
        content="done", tool_call_id="native-call", name="refund"
    ).model_copy(update=change)
    with pytest.raises(ConfigurationError) as error:
        freeze_message(result, invocation=invocation, attempt=attempt)
    assert error.value.reason == "invalid_tool_result"


@pytest.mark.parametrize(
    "field,value",
    [
        ("thread_id", "foreign"),
        ("deployment_id", "foreign"),
        ("tool_call_id", "other"),
        ("tool_name", "other"),
        ("native_call_json", '{"id":"","name":"refund"}'),
    ],
)
def test_foreign_or_altered_attempt_cannot_reconstruct_native_message(field, value):
    attempt, invocation = fixture()
    setattr(attempt, field, value)
    with pytest.raises(ConfigurationError) as error:
        tool_message(attempt, invocation=invocation)
    assert error.value.reason == "durable_attempt_conflict"


def deep_fixture(path):
    pytest.importorskip("deepagents")
    from dataclasses import replace
    from typing import Annotated
    from uuid import uuid4

    from langchain_core.tools import InjectedToolArg, tool

    from openeight.deepagents._continuation import (
        DurableCallContext,
        durable_attempt_key,
    )
    from openeight.deepagents._deployment import (
        BoundCatalogSnapshot,
        BoundExecutionContract,
    )
    from openeight.deepagents.durable import DurableContinuation
    from openeight.langchain._bindings import AdapterBindings

    @tool
    def refund(value: str, idempotency_key: Annotated[str, InjectedToolArg]) -> str:
        """Refund one order."""
        return value

    bindings = AdapterBindings(
        {("first",): (refund,), ("second",): (refund.model_copy(),)}, deep=True
    )
    contracts = tuple(
        BoundExecutionContract(
            binding.child_path,
            binding.native_name,
            binding.facts.name,
            str(uuid4()),
            str(uuid4()),
            binding.schema_bytes.decode(),
            "business_write",
            None,
        )
        for binding in bindings.native.values()
    )
    contract = next(c for c in contracts if c.child_path == path)
    attempt, invocation = fixture()
    namespace = "tools:11111111-1111-4111-8111-111111111111|tools:22222222-2222-4222-8222-222222222222"
    context = DurableCallContext(path, namespace, "parent-key", "business_write")
    key = durable_attempt_key(context, "native-call")
    attempt.idempotency_key = str(uuid4())
    attempt.native_call_json = json.dumps(
        {
            "id": "native-call",
            "name": "refund",
            "args": {"value": "order", "idempotency_key": attempt.idempotency_key},
        }
    )
    attempt.tool_call_id = key
    attempt.tool_name = contract.catalog_name
    attempt.tool_id, attempt.tool_version_id = (
        contract.tool_id,
        contract.tool_version_id,
    )
    attempt.decision_id = "decision"
    invocation.deployment_id = "deployment"
    invocation.owner._integration = "deepagents"
    invocation.owner.catalog = bindings
    invocation.owner._bound_catalog = BoundCatalogSnapshot(contracts)
    continuation = DurableContinuation(
        "deployment",
        "thread",
        "decision",
        json.dumps(list(path), separators=(",", ":")),
        namespace,
        "native-call",
        key,
        "business_write",
        "parent-key",
    )
    return attempt, invocation, continuation, replace


@pytest.mark.parametrize("path", [("first",), ("second",)])
def test_deep_results_preserve_native_identity_across_qualified_catalogs(path):
    attempt, invocation, continuation, _ = deep_fixture(path)
    replay = tool_message(attempt, invocation=invocation, continuation=continuation)
    assert replay.tool_call_id == "native-call" and replay.name == "refund"
    assert replay.name != attempt.tool_name
    assert json.loads(
        freeze_message(
            replay, invocation=invocation, attempt=attempt, continuation=continuation
        )
    ) == {"content": "receipt", "status": "success"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("child_path_json", '["second"]'),
        ("checkpoint_namespace", "tools:33333333-3333-4333-8333-333333333333"),
        ("native_tool_call_id", "other"),
        ("attempt_key", "other"),
        ("deployment_id", "other"),
        ("thread_id", "other"),
        ("decision_id", "other"),
    ],
)
def test_deep_reconstruction_rejects_foreign_continuation(field, value):
    attempt, invocation, continuation, replace = deep_fixture(("first",))
    with pytest.raises(ConfigurationError) as caught:
        tool_message(
            attempt,
            invocation=invocation,
            continuation=replace(continuation, **{field: value}),
        )
    assert caught.value.reason == "durable_attempt_conflict"


def test_deep_reconstruction_requires_current_scoped_catalog_binding():
    pytest.importorskip("deepagents")
    from dataclasses import replace

    from openeight.deepagents._deployment import BoundCatalogSnapshot

    attempt, invocation, continuation, _ = deep_fixture(("first",))
    contracts = invocation.owner._bound_catalog.contracts
    invocation.owner._bound_catalog = BoundCatalogSnapshot(
        tuple(replace(c, tool_version_id="different") for c in contracts)
    )
    with pytest.raises(ConfigurationError) as caught:
        tool_message(attempt, invocation=invocation, continuation=continuation)
    assert caught.value.reason == "durable_attempt_conflict"
