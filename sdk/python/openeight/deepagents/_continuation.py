"""Collision-resistant immutable attempt identity for native child invocations."""

import hashlib
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

from ._deployment import canonical


@dataclass(frozen=True)
class DurableCallContext:
    child_path: tuple[str, ...]
    checkpoint_namespace: str
    parent_attempt_key: str | None
    kind: Literal["read", "business_write", "state_write", "delegation"]


CURRENT_DURABLE_CALL = ContextVar("openeight_durable_call", default=None)


def capture_durable_call_context(request, *, invocation, native, parent_attempt_key):
    from openeight.errors import ConfigurationError

    observation = invocation.native_observations.observations.get(
        native.checkpoint_namespace
    )
    if (
        observation is None
        or observation.identity != native
        or observation.native_call_id != request.tool_call.get("id")
    ):
        raise ConfigurationError("invalid_resume_correlation")
    binding = invocation.owner.catalog.resolve(
        native.child_path, request.tool_call.get("name")
    )
    if binding is None or not binding.matches(
        request.tool, request.tool_call.get("name")
    ):
        raise ConfigurationError("tool_binding_changed")
    catalog = invocation.owner._bound_catalog
    if catalog is None:
        raise ConfigurationError("durable_deployment_unbound")
    contracts = [
        c
        for c in catalog.contracts
        if c.child_path == native.child_path
        and c.native_name == request.tool_call.get("name")
    ]
    if len(contracts) != 1:
        raise ConfigurationError("governance_not_ready")
    contract = contracts[0]
    if (
        contract.catalog_name != binding.facts.name
        or contract.input_schema_json != binding.schema_bytes.decode()
        or contract.recovery_kind
        not in {"read", "business_write", "state_write", "delegation"}
    ):
        raise ConfigurationError("durable_deployment_mismatch")
    generated = invocation.owner._compatibility.generated_recovery_kind(binding)
    if generated is not None and contract.recovery_kind != generated:
        raise ConfigurationError("unsupported_tool_definition")
    if (not native.ancestor_namespaces) != (parent_attempt_key is None):
        raise ConfigurationError("invalid_resume_correlation")
    return DurableCallContext(
        native.child_path,
        native.checkpoint_namespace,
        parent_attempt_key,
        contract.recovery_kind,
    )


def durable_attempt_key(context, native_tool_call_id):
    value = [
        "openeight-deep-attempt-v1",
        list(context.child_path),
        context.checkpoint_namespace,
        native_tool_call_id,
    ]
    return "deep-v1:" + hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def public_native_call(attempt, continuation):
    """Remove only a validated SDK-owned business key from a copied native call."""
    import json

    from openeight.errors import ConfigurationError

    from ._deployment import uuid

    try:
        call = json.loads(attempt.native_call_json)
        if type(call) is not dict or type(call.get("args")) is not dict:
            raise ValueError
        if continuation.kind == "business_write":
            uuid(attempt.idempotency_key)
            if call["args"].pop("idempotency_key") != attempt.idempotency_key:
                raise ValueError
        elif (
            getattr(attempt, "idempotency_key", None) is not None
            or "idempotency_key" in call["args"]
        ):
            raise ValueError
        return call
    except (ConfigurationError, ValueError, KeyError, TypeError, AttributeError):
        raise ConfigurationError("durable_attempt_conflict") from None


def frozen_native_identity(attempt, *, invocation, continuation):
    """Resolve a frozen native result through this deployment's admitted binding."""
    import json

    from openeight.errors import ConfigurationError

    from ._deployment import parse
    from ._frameworks._identity import _namespace_parts

    try:
        native = json.loads(attempt.native_call_json)
        path = parse(continuation.child_path_json)
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
            or type(native) is not dict
            or type(native.get("id")) is not str
            or not native["id"]
            or len(native["id"].encode()) > 256
            or type(native.get("name")) is not str
            or not native["name"]
            or len(native["name"].encode()) > 256
            or attempt.deployment_id != invocation.deployment_id
            or attempt.thread_id != invocation.thread_id
            or continuation.deployment_id != attempt.deployment_id
            or continuation.thread_id != attempt.thread_id
            or continuation.decision_id != attempt.decision_id
            or continuation.kind not in {"business_write", "state_write", "delegation"}
            or (len(parts) == 1) != (continuation.parent_attempt_key is None)
            or (not path) != (len(parts) == 1)
            or native["id"] != continuation.native_tool_call_id
            or attempt.tool_call_id != continuation.attempt_key
            or durable_attempt_key(context, native["id"]) != continuation.attempt_key
        ):
            raise ValueError
        public_native_call(attempt, continuation)
        owner = invocation.owner
        binding = owner.catalog.resolve(tuple(path), native["name"])
        contracts = [
            c
            for c in owner._bound_catalog.contracts
            if c.child_path == tuple(path) and c.native_name == native["name"]
        ]
        if (
            binding is None
            or len(contracts) != 1
            or not binding.matches(binding.tool, native["name"])
        ):
            raise ValueError
        contract = contracts[0]
        if (
            contract.catalog_name != binding.facts.name
            or contract.catalog_name != attempt.tool_name
            or contract.tool_id != attempt.tool_id
            or contract.tool_version_id != attempt.tool_version_id
            or contract.input_schema_json != binding.schema_bytes.decode("utf-8")
            or contract.recovery_kind != continuation.kind
        ):
            raise ValueError
        return native["id"], native["name"]
    except (
        ConfigurationError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        UnicodeError,
    ):
        raise ConfigurationError("durable_attempt_conflict") from None
