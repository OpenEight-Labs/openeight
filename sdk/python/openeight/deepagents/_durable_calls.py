"""Prepare path-scoped durable attempts from admitted read-only contracts."""

import json
from copy import deepcopy
from uuid import uuid4

from openeight import ConfigurationError, Event
from openeight._json import decision_envelope
from openeight.langchain._callbacks import bounded_name
from openeight.langchain._injected import hidden_action_key
from openeight.langchain._runtime import tool_parent_event, utc_timestamp
from openeight.langchain.durable import DurableAttempt
from openeight.telemetry import uuid7
from openeight.tools import collect_selected_values

from ._continuation import durable_attempt_key, frozen_native_identity
from ._deployment import BoundExecutionContract, canonical
from .durable import DurableContinuation


def validate_call(invocation, request, *, call_context):
    if call_context is None:
        raise ConfigurationError("invalid_resume_correlation")
    try:
        call = deepcopy(request.tool_call)
        binding = invocation.owner.catalog.resolve(
            call_context.child_path, call.get("name")
        )
        if binding is None or not binding.matches(request.tool, call.get("name")):
            raise ConfigurationError("tool_binding_changed")
        matches = [
            c
            for c in invocation.owner._bound_catalog.contracts
            if c.child_path == call_context.child_path
            and c.native_name == call.get("name")
        ]
        if len(matches) != 1:
            raise ConfigurationError("governance_not_ready")
        contract = matches[0]
        if (
            contract.recovery_kind != call_context.kind
            or contract.catalog_name != binding.facts.name
            or contract.input_schema_json != binding.schema_bytes.decode()
        ):
            raise ConfigurationError("durable_deployment_mismatch")
        if (
            type(call.get("id")) is not str
            or not call["id"]
            or len(call["id"].encode()) > 256
            or type(call.get("args")) is not dict
            or "idempotency_key" in call["args"]
        ):
            raise ConfigurationError("invalid_input")
        public = request.tool.tool_call_schema
        schema = public.model_json_schema()
        if set(call["args"]) - set(schema.get("properties", {})):
            raise ConfigurationError("invalid_input")
        validated = public.model_validate(call["args"], strict=True).model_dump(
            exclude_unset=True
        )
        if canonical(validated) != canonical(call["args"]):
            raise ConfigurationError("invalid_input")
        return call, contract.catalog_name, contract
    except ConfigurationError:
        raise
    except Exception:
        raise ConfigurationError("invalid_input") from None


def prepare(invocation, request, execution_contract, *, call_context):
    call, name, admitted = validate_call(invocation, request, call_context=call_context)
    ready = admitted.readiness
    if not ready.eligible or not ready.policy_ready:
        raise ConfigurationError("governance_not_ready")
    if (
        ready.registration_status != "registered"
        or call_context.kind == "read"
        or not ready.governed
        or ready.reason != "ready"
        or ready.tool_id != admitted.tool_id
        or ready.tool_version_id != admitted.tool_version_id
        or ready.name != name
        or not ready.policy_contract_set_digest
        or not ready.policy_input_contracts
    ):
        raise ConfigurationError("governance_unconfigured")
    business_write = call_context.kind == "business_write"
    if business_write:
        if not hidden_action_key(request.tool):
            raise ConfigurationError("unsupported_tool_definition")
    else:
        binding = invocation.owner.catalog.resolve(
            call_context.child_path, call["name"]
        )
        if (
            invocation.owner._compatibility.generated_recovery_kind(binding)
            != call_context.kind
        ):
            raise ConfigurationError("unsupported_tool_definition")
    key = durable_attempt_key(call_context, call["id"])
    previous = invocation.store.get_attempt(invocation.thread_id, key)
    if previous is not None:
        if (
            previous.status not in {"succeeded", "failed", "blocked"}
            and previous.policy_contract_set_digest != ready.policy_contract_set_digest
        ):
            raise ConfigurationError("governance_not_ready")
        continuation = invocation.store.continuation(invocation.thread_id, key)
        frozen_native_identity(
            previous, invocation=invocation, continuation=continuation
        )
        if (
            continuation.parent_attempt_key != call_context.parent_attempt_key
            or continuation.kind != call_context.kind
        ):
            raise ConfigurationError("durable_attempt_conflict")
        effective = deepcopy(call)
        if previous.idempotency_key:
            effective["args"]["idempotency_key"] = previous.idempotency_key
        if previous.native_call_json != canonical(effective):
            raise ConfigurationError("durable_attempt_conflict")
        return previous, effective, admitted
    contract = execution_contract
    if not isinstance(contract, BoundExecutionContract) or contract != admitted:
        raise ConfigurationError("governance_not_ready")
    if call_context.checkpoint_namespace in invocation.recovery_namespaces:
        raise ConfigurationError("durable_attempt_missing")
    thread = invocation.store.load(invocation.thread_id)
    if (
        len(invocation.store.attempts(invocation.thread_id)) + thread.read_calls
        >= invocation.owner.control.durable.max_tool_attempts
    ):
        raise ConfigurationError("durable_limit_exhausted")
    selected = decision_envelope(
        {},
        ready.policy_contract_set_digest,
        collect_selected_values(call["args"], ready),
    ).decode()
    idempotency = str(uuid4()) if business_write else None
    if idempotency is not None:
        call["args"]["idempotency_key"] = idempotency
    decision_id, event_id, stamp = uuid7(), uuid7(), utc_timestamp()
    parent = tool_parent_event(invocation, request)
    if call_context.parent_attempt_key is not None:
        parent_attempt = invocation.store.get_attempt(
            invocation.thread_id, call_context.parent_attempt_key
        )
        if parent_attempt is None or parent_attempt.status != "executing":
            raise ConfigurationError("invalid_resume_correlation")
        parent = parent_attempt.event_id
    event = Event(
        bounded_name(name),
        "tool_call",
        "invoke",
        stamp,
        "success",
        parent_event_id=parent,
        decision_id=decision_id,
        input=json.loads(selected),
        tool_id=contract.tool_id,
        tool_version_id=contract.tool_version_id,
        tool_name=name,
    )._as_write()
    event.pop("output_json", None)
    event.update(id=event_id, run_id=invocation.run_id)
    attempt = DurableAttempt(
        invocation.deployment_id,
        decision_id,
        invocation.thread_id,
        key,
        name,
        contract.tool_id,
        contract.tool_version_id,
        ready.policy_contract_set_digest,
        stamp,
        event_id,
        canonical(call),
        selected,
        canonical(event),
        stamp,
        stamp,
        idempotency_key=idempotency,
    )
    continuation = DurableContinuation(
        invocation.deployment_id,
        invocation.thread_id,
        decision_id,
        canonical(list(call_context.child_path)),
        call_context.checkpoint_namespace,
        call["id"],
        key,
        call_context.kind,
        call_context.parent_attempt_key,
    )
    return invocation.store.prepare_continuation(attempt, continuation), call, contract
