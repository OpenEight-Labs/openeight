"""Validate parent authority against independent native invocation observations."""

import json
from contextvars import ContextVar
from dataclasses import dataclass

from openeight.errors import ConfigurationError


@dataclass(frozen=True)
class ParentDelegation:
    deployment_id: str
    thread_id: str
    attempt_key: str
    child_path: tuple[str, ...]
    target_child_path: tuple[str, ...]
    checkpoint_namespace: str


CURRENT_PARENT_DELEGATION = ContextVar("openeight_parent_delegation", default=None)


def _path(raw):
    from ._deployment import parse

    try:
        value = parse(raw)
    except ConfigurationError:
        raise ConfigurationError("invalid_resume_correlation") from None
    if (
        type(value) is not list
        or len(value) > 16
        or any(type(v) is not str or not v for v in value)
    ):
        raise ConfigurationError("invalid_resume_correlation")
    return tuple(value)


def parent_delegation(attempt, continuation, *, target_child_path):
    if (
        continuation.kind != "delegation"
        or attempt.deployment_id != continuation.deployment_id
        or attempt.thread_id != continuation.thread_id
        or attempt.decision_id != continuation.decision_id
        or attempt.tool_call_id != continuation.attempt_key
        or attempt.status != "executing"
    ):
        raise ConfigurationError("invalid_resume_correlation")
    return ParentDelegation(
        attempt.deployment_id,
        attempt.thread_id,
        attempt.tool_call_id,
        _path(continuation.child_path_json),
        target_child_path,
        continuation.checkpoint_namespace,
    )


def validate_parent_delegation(parent, *, invocation, native):
    try:
        if not native.ancestor_namespaces:
            if (
                parent is not None
                or native.child_path
                or native.parent_native_tool_call_id is not None
            ):
                raise ValueError
            return None
        if (
            parent is None
            or parent.deployment_id != invocation.deployment_id
            or parent.thread_id != invocation.thread_id
            or parent.target_child_path != native.child_path
        ):
            raise ValueError
        chain = []
        seen = set()
        current = parent.attempt_key
        target = native.child_path
        while current is not None:
            if current in seen or len(seen) >= 16:
                raise ValueError
            seen.add(current)
            continuation = invocation.store.continuation(invocation.thread_id, current)
            attempt = invocation.store.get_attempt(invocation.thread_id, current)
            if (
                continuation is None
                or attempt is None
                or continuation.kind != "delegation"
                or continuation.deployment_id != invocation.deployment_id
                or continuation.thread_id != invocation.thread_id
                or continuation.decision_id != attempt.decision_id
                or continuation.attempt_key != attempt.tool_call_id
                or attempt.deployment_id != invocation.deployment_id
                or attempt.thread_id != invocation.thread_id
                or attempt.status != "executing"
            ):
                raise ValueError
            path = _path(continuation.child_path_json)
            call = json.loads(attempt.native_call_json)
            from ._continuation import DurableCallContext, durable_attempt_key

            expected_key = durable_attempt_key(
                DurableCallContext(
                    path,
                    continuation.checkpoint_namespace,
                    continuation.parent_attempt_key,
                    "delegation",
                ),
                continuation.native_tool_call_id,
            )
            if expected_key != current:
                raise ValueError
            observation = invocation.native_observations.observations.get(
                continuation.checkpoint_namespace
            )
            if (
                call.get("id") != continuation.native_tool_call_id
                or call.get("name") != "task"
                or (*path, call.get("args", {}).get("subagent_type")) != target
                or observation is None
                or observation.native_call_id != continuation.native_tool_call_id
                or observation.identity.child_path != path
                or observation.target_child_path != target
            ):
                raise ValueError
            topology = invocation.owner._composition["topology"]
            node = next(
                (node for node in topology if tuple(node["path"]) == path), None
            )
            if node is None or list(target) not in node["delegates_to"]:
                raise ValueError
            chain.append(continuation.checkpoint_namespace)
            if current == parent.attempt_key:
                if (
                    path != parent.child_path
                    or continuation.checkpoint_namespace != parent.checkpoint_namespace
                    or continuation.native_tool_call_id
                    != native.parent_native_tool_call_id
                ):
                    raise ValueError
            target = path
            current = continuation.parent_attempt_key
        if tuple(reversed(chain)) != native.ancestor_namespaces:
            raise ValueError
        return parent.attempt_key
    except (ValueError, KeyError, TypeError, AttributeError, StopIteration):
        raise ConfigurationError("invalid_resume_correlation") from None
