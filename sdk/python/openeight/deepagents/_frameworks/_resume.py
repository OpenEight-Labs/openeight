"""Correlate native interrupts to exact admitted child attempts and parents."""

import json

from langgraph.types import Command

from openeight.errors import ConfigurationError

from .._continuation import frozen_native_identity, public_native_call
from .._parents import parent_delegation, validate_parent_delegation


def waiting_task(snapshot, *, invocation, attempt, require_waiting=True):
    if snapshot.native.observations is not invocation.native_observations:
        raise ValueError
    continuation = invocation.store.continuation(
        invocation.thread_id, attempt.tool_call_id
    )
    frozen_native_identity(attempt, invocation=invocation, continuation=continuation)
    candidates = [
        t
        for t in snapshot.native.tasks
        if t.namespace == continuation.checkpoint_namespace
    ]
    statuses = (
        {"waiting"}
        if require_waiting
        else {
            "waiting",
            "executing",
            "reconciliation_required",
            "succeeded",
            "failed",
            "blocked",
        }
    )
    if len(candidates) != 1 or attempt.status not in statuses:
        raise ValueError
    task = candidates[0]
    native = invocation.native_observations.observations.get(task.namespace)
    if (
        native is None
        or native.native_call_id != continuation.native_tool_call_id
        or task.call != public_native_call(attempt, continuation)
        or list(task.child_path) != json.loads(continuation.child_path_json)
    ):
        raise ValueError
    parent = None
    if continuation.parent_attempt_key is not None:
        parent_continuation = invocation.store.continuation(
            invocation.thread_id, continuation.parent_attempt_key
        )
        parent_attempt = invocation.store.get_attempt(
            invocation.thread_id, continuation.parent_attempt_key
        )
        if parent_continuation is None or parent_attempt is None:
            raise ValueError
        parent = parent_delegation(
            parent_attempt,
            parent_continuation,
            target_child_path=native.identity.child_path,
        )
    parent_key = validate_parent_delegation(
        parent, invocation=invocation, native=native.identity
    )
    if parent_key != continuation.parent_attempt_key:
        raise ValueError
    return task, native, continuation


def resume_input(snapshot, *, invocation, decision_ids=None):
    try:
        if snapshot.native.observations is not invocation.native_observations:
            raise ValueError
        records = tuple(invocation.store.attempts(invocation.thread_id))
        attempts = {a.decision_id: a for a in records}
        if len(attempts) != len(records) or any(
            a.deployment_id != invocation.deployment_id
            or a.thread_id != invocation.thread_id
            for a in records
        ):
            raise ValueError
        values = {}
        seen = {}
        for projected in snapshot.native.tasks:
            for interrupt in projected.task.interrupts:
                value = interrupt.value
                if (
                    type(interrupt.id) is not str
                    or not interrupt.id
                    or type(value) is not dict
                    or set(value) != {"decision_id"}
                    or type(value["decision_id"]) is not str
                    or value["decision_id"] not in attempts
                ):
                    raise ValueError
                if interrupt.id in seen and seen[interrupt.id] != value:
                    raise ValueError
                seen[interrupt.id] = value
                attempt = attempts[value["decision_id"]]
                task, native, continuation = waiting_task(
                    snapshot,
                    invocation=invocation,
                    attempt=attempt,
                    require_waiting=False,
                )
                if not any(
                    i.id == interrupt.id and i.value == value
                    for i in task.task.interrupts
                ):
                    raise ValueError
                occurrence = invocation.native_observations.observations.get(
                    projected.namespace
                )
                if (
                    projected.namespace
                    not in {task.namespace, *native.identity.ancestor_namespaces}
                    or occurrence is None
                    or occurrence.native_call_id != projected.call.get("id")
                    or occurrence.identity.child_path != projected.child_path
                ):
                    raise ValueError
                # Native interrupt writes can outlive their journaled resume and
                # settlement. Correlate every occurrence, but only supply a new
                # resume value for an attempt that is still waiting.
                if attempt.status == "waiting":
                    values[interrupt.id] = value
        if decision_ids is not None:
            values = {
                identifier: value
                for identifier, value in values.items()
                if value["decision_id"] in decision_ids
            }
        return Command(resume=values) if values else None
    except (ConfigurationError, ValueError, TypeError, KeyError, AttributeError):
        raise ConfigurationError("invalid_resume_correlation") from None
