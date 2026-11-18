"""Only the three admitted async commands can advance durable native agents."""

import asyncio
import json
from dataclasses import replace
from importlib.metadata import version

from openeight import ConfigurationError, ProtocolError
from openeight._framework import (
    detect_framework,
    load_compatibility,
    select_compatibility,
)
from openeight._framework._registry import load_registry
from openeight.telemetry import uuid7

from ._deployment import checked_record
from ._durable_runtime import DurableInvocation, canonical
from ._runtime import invocation_context, utc_timestamp
from .durable import (
    CommandSettlement,
    DurableAgentResult,
    DurableObserveRecord,
    DurableTransition,
)

PINS = next(
    row["packages"]
    for row in load_registry()["entries"]
    if row["id"] == "langchain-durable-01"
)


def validate_durable_graph(graph, *, integration="langchain"):
    try:
        selection = select_compatibility(
            detect_framework(integration, version=version),
            required=frozenset({"durable"}),
        )
    except ConfigurationError:
        if integration != "langchain":
            raise
        # Preserve the public error for unsupported LangChain versions.
        raise ConfigurationError("unsupported_framework_version") from None
    load_compatibility(selection).validate_durable_saver(graph)


def _message(kind, value):
    if kind == "resume":
        return None
    if kind == "reply":
        message = value
    else:
        if (
            type(value) is not dict
            or set(value) != {"messages"}
            or type(value["messages"]) is not list
            or len(value["messages"]) != 1
        ):
            raise ConfigurationError("invalid_input")
        item = value["messages"][0]
        if (
            type(item) is not dict
            or set(item) != {"role", "content"}
            or item["role"] != "user"
        ):
            raise ConfigurationError("invalid_input")
        message = item["content"]
    if type(message) is not str or not message.strip() or len(message.encode()) > 8192:
        raise ConfigurationError("invalid_input")
    return message


def _visible(snapshot, command_id, *, compatibility):
    result = compatibility.visible_output(snapshot, command_id=command_id)
    if any(type(body) is not str or len(body.encode()) > 8192 for body in result):
        raise ConfigurationError("invalid_output")
    return result


def _result(store, thread, output=()):
    return DurableAgentResult(
        thread.id,
        thread.run_id,
        thread.status,
        tuple(
            a.decision_id
            for a in checked_record(
                store.attempts(thread.id), kind="attempt", thread_id=thread.id
            )
            if a.status == "waiting"
        ),
        output,
    )


def _resume_input(snapshot, store, thread_id, *, invocation):
    if invocation.thread_id != thread_id or invocation.store is not store:
        raise ConfigurationError("invalid_resume_correlation")
    return invocation.owner._compatibility.resume_input(
        snapshot, invocation=invocation, continuations=()
    )


def _recovered_command(store, thread_id, command):
    current = command
    seen = {command.id}
    while current.recover_command_id:
        current = checked_record(
            store.command(thread_id, current.recover_command_id),
            kind="command",
            thread_id=thread_id,
            command_id=current.recover_command_id,
        )
        if current.id in seen:
            raise ConfigurationError("durable_command_conflict")
        seen.add(current.id)
    return current if current.id != command.id else None


async def execute_durable(owner, kind, value, thread_id, command_id):
    owner._ensure_open()
    if owner.control is None or owner.control.durable is None:
        raise ConfigurationError("unsupported_execution_method")
    message = _message(kind, value)
    task = asyncio.create_task(_execute(owner, kind, message, thread_id, command_id))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Cancellation cannot release the OS lock while an admitted handler or
        # credential-bearing request still owns the operation.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if not task.cancelled():
            task.exception()
        raise


async def _execute(owner, kind, message, thread_id, command_id):
    from contextlib import ExitStack

    store = owner.control.durable.store
    with ExitStack() as ownership:
        from ._deployment import record_scope

        ownership.enter_context(record_scope(owner))
        if owner._integration == "deepagents":
            ownership.enter_context(store.acquire_deployment())
        elif getattr(owner.control.durable, "deployment_binding", None) is not None:
            from ._deployment import ownership as deployment_ownership
            from ._deployment import validate_admission

            ownership.enter_context(
                deployment_ownership(owner.control.durable.deployment_binding)
            )
            await validate_admission(owner)
        ownership.enter_context(store.acquire(thread_id))
        result = await _execute_admitted(owner, kind, message, thread_id, command_id)
        return await terminal_structured_result(owner, thread_id, command_id, result)


async def terminal_structured_result(owner, thread_id, command_id, result):
    """Read only this command's committed checkpoint while retaining command ownership."""
    compatibility = owner._compatibility
    if getattr(compatibility, "_response_spec", None) is None:
        return result
    command = checked_record(
        owner.control.durable.store.command(thread_id, command_id),
        kind="command",
        thread_id=thread_id,
        command_id=command_id,
    )
    if command.status != "completed":
        return result
    checkpoint_id = command.output_checkpoint_id
    if checkpoint_id is None:
        raise ConfigurationError("durable_checkpoint_missing")
    snapshot = await compatibility.read_checkpoint(
        owner._graph,
        {
            "configurable": {"thread_id": thread_id, "checkpoint_id": checkpoint_id},
        },
        checkpoint_id=checkpoint_id,
    )
    if snapshot.checkpoint_id != checkpoint_id or snapshot.has_next:
        raise ConfigurationError("durable_checkpoint_conflict")
    present, value = compatibility.structured_response(snapshot)
    return replace(result, has_structured_response=present, structured_response=value)


async def _execute_admitted(owner, kind, message, thread_id, command_id):
    """Advance one admitted command while the caller retains deployment/thread ownership."""
    store = owner.control.durable.store
    if owner._integration == "deepagents":
        from openeight.deepagents._admission import _validate_durable_admission

        await _validate_durable_admission(owner)
    thread = checked_record(
        store.load(thread_id), kind="thread", thread_id=thread_id, allow_none=True
    )
    if thread is None:
        raise ConfigurationError("durable_thread_missing")
    command = checked_record(
        store.command(thread_id, command_id),
        kind="command",
        thread_id=thread_id,
        command_id=command_id,
    )
    request = json.loads(command.request_json)
    if (
        command.kind != kind
        or request.get("command_id") != command_id
        or (kind != "resume" and request.get("message") != message)
    ):
        raise ConfigurationError("durable_command_conflict")
    if command.status != "accepted":
        if command.status == "running":
            raise ConfigurationError("durable_recovery_required")
        return _result(store, thread)
    if kind == "start" and (
        thread.checkpoint_id
        or thread.active_command_id
        or thread.read_calls
        or thread.model_calls
        or checked_record(
            store.attempts(thread_id), kind="attempt", thread_id=thread_id
        )
    ):
        raise ConfigurationError("durable_command_conflict")
    if kind == "reply" and thread.status != "waiting_customer":
        raise ConfigurationError("durable_command_conflict")
    invocation = DurableInvocation(owner, thread)
    with owner.lock:
        owner._ensure_open()
        if len(owner.roots) >= 1000 or not owner.worker.reserve(512):
            raise ConfigurationError("execution_busy")
        owner.roots.add(invocation)
    try:
        command = checked_record(
            store.begin_command(
                thread_id, command_id, command.revision, thread.revision
            ),
            kind="command",
            thread_id=thread_id,
            command_id=command_id,
        )
        await owner._run_client_call(invocation.start)
        await invocation.drain()
        configuration = owner._prepare_runnable_config(
            {"configurable": {"thread_id": thread_id}}, invocation
        )
        graph = owner._graph
        compatibility = owner._compatibility
        invocation.recovering = kind == "resume"
        snapshot = await compatibility.read_checkpoint(graph, configuration)
        invocation.adopt_checkpoint(snapshot)
        await invocation.prepare_generated_recovery(snapshot)
        original = _recovered_command(store, thread_id, command)
        execution = original or command
        invocation.recovering = kind == "resume"
        request = json.loads(execution.request_json)
        snapshot_id = snapshot.checkpoint_id
        if thread.checkpoint_id is not None and snapshot_id is None:
            raise ConfigurationError("durable_checkpoint_missing")
        # Approval state alone never admits a handler; this read only avoids
        # consuming a native resume value while an Approval is still pending.
        pending = [
            a
            for a in checked_record(
                store.attempts(thread_id), kind="attempt", thread_id=thread_id
            )
            if a.status == "waiting"
        ]
        approval_states = {}
        for attempt in pending:
            state = await owner._run_client_call(
                lambda attempt=attempt: owner.client.approval_state(attempt.decision_id)
            )
            approval_states[attempt.decision_id] = state
        if owner._integration == "deepagents":
            if any(
                state not in {"pending", "approved", "rejected", "expired"}
                for state in approval_states.values()
            ):
                raise ProtocolError()
            invocation.resume_decision_ids = frozenset(
                decision
                for decision, state in approval_states.items()
                if state != "pending"
            )
            still_waiting = bool(pending) and not invocation.resume_decision_ids
        else:
            still_waiting = any(
                state == "pending" for state in approval_states.values()
            )
        output = ()
        if thread.run_ended_at:
            # Fixed completion permits delivery retry, never graph execution.
            await invocation.drain()
            if execution.kind != "resume":
                output = _visible(snapshot, execution.id, compatibility=compatibility)
            elif execution.input_checkpoint_id:
                before = await compatibility.read_checkpoint(
                    graph,
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_id": execution.input_checkpoint_id,
                        }
                    },
                )
                output = compatibility.output_since(snapshot, before=before)
        elif still_waiting:
            pass
        else:
            execution_input = None
            present = execution.id in snapshot.message_ids
            if execution.kind in {"start", "reply"}:
                if not execution.input_recorded:
                    if present:
                        raise ConfigurationError("durable_checkpoint_conflict")
                    execution = checked_record(
                        store.record_command_input(
                            thread_id, execution.id, execution.revision, snapshot_id
                        ),
                        kind="command",
                        thread_id=thread_id,
                        command_id=execution.id,
                    )
                    if not original:
                        command = execution
                if not present:
                    if execution.input_checkpoint_id != snapshot_id:
                        raise ConfigurationError("durable_checkpoint_conflict")
                    prior = store.prior_customer_inputs(thread_id, execution.id)
                    execution_input = compatibility.message_input(
                        tuple(
                            (message_id, body)
                            for message_id, body in prior
                            if message_id not in snapshot.message_ids
                        )
                        + ((execution.id, request["message"]),)
                    )
                elif snapshot.has_next:
                    invocation.recovering = bool(original)
                    execution_input = _resume_input(
                        snapshot, store, thread_id, invocation=invocation
                    )
                else:
                    execution_input = "already_finished"
            else:
                if not snapshot_id:
                    raise ConfigurationError("durable_checkpoint_missing")
                if not execution.input_recorded:
                    execution = checked_record(
                        store.record_command_input(
                            thread_id, execution.id, execution.revision, snapshot_id
                        ),
                        kind="command",
                        thread_id=thread_id,
                        command_id=execution.id,
                    )
                    if not original:
                        command = execution
                execution_input = (
                    (_resume_input(snapshot, store, thread_id, invocation=invocation))
                    if snapshot.has_next
                    else "already_finished"
                )
            if execution_input != "already_finished":
                with invocation_context(invocation):
                    await compatibility.invoke_durable(
                        graph, execution_input, configuration
                    )
                snapshot = await compatibility.read_checkpoint(graph, configuration)
                invocation.adopt_checkpoint(snapshot)
            await invocation.settle_callbacks()
            if execution.kind != "resume":
                output = _visible(snapshot, execution.id, compatibility=compatibility)
            else:
                before = await compatibility.read_checkpoint(
                    graph,
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_id": execution.input_checkpoint_id,
                        }
                    },
                )
                output = compatibility.output_since(snapshot, before=before)
                if any(len(body.encode()) > 8192 for body in output):
                    raise ConfigurationError("invalid_output")
        if any(type(body) is not str or len(body.encode()) > 8192 for body in output):
            raise ConfigurationError("invalid_output")
        thread = checked_record(
            store.load(thread_id), kind="thread", thread_id=thread_id, allow_none=True
        )
        checkpoint_id = snapshot.checkpoint_id
        attempts = checked_record(
            store.attempts(thread_id), kind="attempt", thread_id=thread_id
        )
        pending_delegations = compatibility.pending_delegations(
            snapshot, invocation=invocation
        )
        if any(
            a.status in {"prepared", "executing", "reconciliation_required"}
            and not (a.status == "executing" and a.tool_call_id in pending_delegations)
            for a in attempts
        ):
            raise ConfigurationError("durable_reconciliation_required")
        waiting = still_waiting or any(a.status == "waiting" for a in attempts)
        if waiting:
            disposition, status = "waiting", "waiting"
        elif thread.status == "completed":
            disposition, status = "completed", "completed"
        else:
            disposition = await owner.control.durable.classify_turn(thread_id)
            if disposition not in {"terminal", "waiting_customer"}:
                raise ConfigurationError("invalid_turn_disposition")
            await invocation.drain()
            if disposition == "terminal":
                ts = utc_timestamp()
                thread = checked_record(
                    store.load(thread_id),
                    kind="thread",
                    thread_id=thread_id,
                    allow_none=True,
                )
                payload = {
                    "id": thread.run_id,
                    "started_at": thread.run_started_at,
                    "ended_at": ts,
                    "status": "completed",
                    "attributes_json": "{}",
                }
                checked_record(
                    store.begin_completion(
                        thread_id,
                        thread.revision,
                        ts,
                        DurableObserveRecord(
                            uuid7(), thread_id, "run_complete", canonical(payload), ts
                        ),
                    ),
                    kind="thread",
                    thread_id=thread_id,
                )
                await invocation.drain()
                disposition, status = "completed", "completed"
            else:
                status = "completed"
        await invocation.drain()
        thread = checked_record(
            store.load(thread_id), kind="thread", thread_id=thread_id, allow_none=True
        )
        command = checked_record(
            store.command(thread_id, command_id),
            kind="command",
            thread_id=thread_id,
            command_id=command_id,
        )
        original = (
            checked_record(
                store.command(thread_id, original.id),
                kind="command",
                thread_id=thread_id,
                command_id=original.id,
            )
            if original
            else None
        )
        checked_record(
            store.settle_command(
                thread_id,
                command_id,
                command.revision,
                CommandSettlement(
                    status,
                    thread.revision,
                    disposition,
                    checkpoint_id,
                    output if not original else (),
                    recovered_command_id=original.id if original else None,
                    recovered_command_revision=original.revision if original else None,
                    recovered_status=status if original else None,
                    recovered_turns=output if original else (),
                ),
            ),
            kind="command",
            thread_id=thread_id,
            command_id=command_id,
        )
        return _result(
            store,
            checked_record(
                store.load(thread_id),
                kind="thread",
                thread_id=thread_id,
                allow_none=True,
            ),
            output,
        )
    except BaseException:
        thread = checked_record(
            store.load(thread_id), kind="thread", thread_id=thread_id, allow_none=True
        )
        if (
            thread is not None
            and not thread.run_ended_at
            and thread.status != "completed"
        ):
            checked_record(
                store.transition(
                    thread_id,
                    thread.revision,
                    DurableTransition(
                        status="reconciliation_required", observation_incomplete=True
                    ),
                ),
                kind="thread",
                thread_id=thread_id,
            )
        raise
    finally:
        try:
            await invocation.settle_callbacks()
        finally:
            invocation.release()
