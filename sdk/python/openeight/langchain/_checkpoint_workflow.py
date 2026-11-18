"""Enroll and execute ordinary checkpoint turns under trusted application claims."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

from langgraph.types import Command

from openeight._credential_binding import credential_binding
from openeight._time import utc_timestamp
from openeight.checkpoints import (
    CheckpointBinding,
    CheckpointEnrollment,
    CheckpointExecution,
)
from openeight.telemetry import uuid7

from ._checkpoint_claim import CURRENT_CHECKPOINT_CLAIM, ClaimScope, invalid
from ._checkpoint_interrupt import qualified_interrupt
from ._checkpoint_measurement import configuration_digest


async def application_call(operation):
    failed = False
    result = None
    try:
        result = await operation()
    except Exception:
        failed = True
    if failed:
        invalid()
    return result


def validate_binding(binding, config, variant):
    if type(binding) is not CheckpointBinding:
        invalid()
    for name in ("scope_id", "thread_id", "checkpoint_ns", "graph_revision"):
        value = getattr(binding, name)
        if (
            type(value) is not str
            or (name != "checkpoint_ns" and not value)
            or len(value.encode()) > 4096
            or "\0" in value
        ):
            invalid()
    values = config.get("configurable", {})
    if (
        binding.mode not in {"new", "continue", "adopt"}
        or binding.model_variant != variant
        or values.get("thread_id") != binding.thread_id
        or values.get("checkpoint_ns", "") != binding.checkpoint_ns
        or values.get("checkpoint_map")
    ):
        invalid()


def current_id(value):
    return (
        value.config["configurable"].get("checkpoint_id") if value is not None else None
    )


def quiescent(snapshot, native):
    from langchain_core.messages import AIMessage, ToolMessage

    if snapshot.next or snapshot.tasks or snapshot.interrupts or native.pending_writes:
        return False
    messages = (
        snapshot.values.get("messages", []) if isinstance(snapshot.values, dict) else []
    )
    pending = set()
    for message in messages:
        if isinstance(message, AIMessage):
            if pending:
                return False
            pending.update(call["id"] for call in message.tool_calls)
        elif isinstance(message, ToolMessage):
            pending.discard(message.tool_call_id)
        elif pending:
            return False
    return not pending


async def read_snapshot(scope):
    from langchain_core.messages import BaseMessage

    snapshot = await scope.graph.aget_state(scope.config, subgraphs=True)
    if (
        not isinstance(snapshot.values, dict)
        or type(snapshot.values.get("messages", [])) is not list
    ):
        invalid()
    messages = snapshot.values.get("messages", [])
    if any(
        not isinstance(message, BaseMessage)
        or message.id is not None
        and (type(message.id) is not str or not message.id)
        for message in messages
    ):
        invalid()
    return snapshot


async def prepare_claim(session, claim, value, config):
    owner = session._require()
    selected = (
        session._select_model(config or {})
        if session._runtime_models is not None
        else None
    )
    variant = selected.variant_name if selected is not None else None
    config = session._config(config)
    binding = claim.binding
    validate_binding(binding, config, variant)
    digest = configuration_digest(session, binding.graph_revision)
    scope = ClaimScope(claim, binding, digest, variant)
    scope.native_pending = False
    scope.execution = None
    scope.graph = session._generation_builds[
        variant if variant is not None else "__static__"
    ].graph
    scope.native_hitl_nodes = session._generation_builds[
        variant if variant is not None else "__static__"
    ].composition["checkpoint_hitl_nodes"]
    built = session._generation_builds[variant if variant is not None else "__static__"]
    scope.bindings = built.bindings
    scope.graphs = {(): built.graph}
    if session._integration == "deepagents":
        from openeight.deepagents._frameworks._inspection import native_graphs

        scope.graphs = native_graphs(
            built.graph, {control.child_path: control for control in built.controls}
        )
    scope.config = {
        **config,
        "configurable": {
            "thread_id": binding.thread_id,
            "checkpoint_ns": binding.checkpoint_ns,
        },
    }
    scope.input_checkpoint_id = None
    await scope.assert_held()
    await owner._run_client_call(lambda: owner.client.get_tool_readiness([]))
    enrollment = await application_call(claim.read_binding)
    expected = CheckpointEnrollment(
        scope_id=binding.scope_id,
        thread_id=binding.thread_id,
        checkpoint_ns=binding.checkpoint_ns,
        format_version=1,
        agent_name=session._governance.agent_name,
        endpoint=session._governance.endpoint,
        identity_kind="credential_bound",
        credential_binding_sha256=credential_binding(session._governance.credential),
        graph_revision=binding.graph_revision,
        integration=session._integration,
        configuration_sha256=digest,
        origin="new",
        adoption_checkpoint_id=None,
        adopted_at=None,
    )
    if enrollment is not None:
        if type(enrollment) is not CheckpointEnrollment or enrollment.origin not in {
            "new",
            "adopted",
        }:
            invalid()
        expected = replace(
            expected,
            origin=enrollment.origin,
            adoption_checkpoint_id=enrollment.adoption_checkpoint_id,
            adopted_at=enrollment.adopted_at,
        )
        if (
            enrollment != expected
            or enrollment.origin == "adopted"
            and (not enrollment.adoption_checkpoint_id or not enrollment.adopted_at)
            or enrollment.origin == "new"
            and (
                enrollment.adoption_checkpoint_id is not None
                or enrollment.adopted_at is not None
            )
        ):
            invalid()
    scope.enrollment = expected
    return scope, enrollment, config


async def validate_history(session, scope, enrollment, input, config):
    binding, claim = scope.binding, scope.claim
    native = await session._saver.aget_tuple(scope.config)
    latest = current_id(native)
    selected = config["configurable"].get("checkpoint_id")
    if selected is not None and selected != latest:
        invalid()
    prior = await application_call(claim.last_execution)
    if enrollment is not None and prior is not None and binding.mode == "new":
        invalid()
    if prior is not None:
        if (
            type(prior) is not CheckpointExecution
            or (prior.scope_id, prior.thread_id, prior.checkpoint_ns)
            != (binding.scope_id, binding.thread_id, binding.checkpoint_ns)
            or prior.state not in {"settled", "native_interrupt"}
            or not prior.finished_at
            or not prior.output_checkpoint_id
            or prior.output_checkpoint_id != latest
        ):
            invalid()
    if enrollment is None:
        if prior is not None:
            invalid()
        if binding.mode == "new":
            if native is not None or isinstance(input, Command):
                invalid()
        elif binding.mode == "adopt":
            if native is None or selected is None or isinstance(input, Command):
                invalid()
            snapshot = await read_snapshot(scope)
            if not quiescent(snapshot, native):
                invalid()
            scope.enrollment = replace(
                scope.enrollment,
                origin="adopted",
                adoption_checkpoint_id=latest,
                adopted_at=utc_timestamp(),
            )
        else:
            invalid()
        await scope.assert_held()
        await application_call(lambda: claim.bind(scope.enrollment))
        if await application_call(claim.read_binding) != scope.enrollment:
            invalid()
    elif prior is None:
        if latest != enrollment.adoption_checkpoint_id:
            invalid()
    else:
        marker = (
            native.metadata.get("openeight_checkpoint") if native is not None else None
        )
        if marker != {
            "configuration_sha256": scope.configuration_sha256,
            "graph_revision": binding.graph_revision,
            "model_variant": prior.model_variant,
        }:
            invalid()
    if prior is not None and prior.state == "native_interrupt":
        if (
            prior.model_variant != scope.model_variant
            or not isinstance(input, Command)
            or input.resume is None
            or input.update is not None
            or input.goto
            or input.graph is not None
        ):
            invalid()
        snapshot = await read_snapshot(scope)
        if not snapshot.interrupts or not await qualified_interrupt(session, scope):
            invalid()
        identities = {item.id for item in snapshot.interrupts}
        resume = input.resume
        if type(resume) is not dict or not resume:
            invalid()
        if set(resume) <= identities:
            if any(
                type(response) is not dict or set(response) != {"decisions"}
                for response in resume.values()
            ):
                invalid()
        elif len(identities) != 1 or set(resume) != {"decisions"}:
            invalid()
        scope.native_pending = True
    elif isinstance(input, Command):
        invalid()
    if enrollment is not None and binding.mode == "adopt":
        # Re-entering after enrollment but before begin is the same cutover, never a new adoption.
        if (
            prior is not None
            or latest != enrollment.adoption_checkpoint_id
            or selected != latest
        ):
            invalid()
    scope.input_checkpoint_id = latest


async def begin_execution(scope, run_id):
    await scope.assert_held()
    record = CheckpointExecution(
        scope_id=scope.binding.scope_id,
        thread_id=scope.binding.thread_id,
        checkpoint_ns=scope.binding.checkpoint_ns,
        execution_id=uuid7(),
        run_id=run_id,
        started_at=utc_timestamp(),
        finished_at=None,
        state="running",
        input_checkpoint_id=scope.input_checkpoint_id,
        output_checkpoint_id=None,
        model_variant=scope.model_variant,
    )
    await application_call(lambda: scope.claim.begin(record))
    if await application_call(scope.claim.last_execution) != record:
        invalid()
    scope.execution, scope.execution_id, scope.read_only = (
        record,
        record.execution_id,
        False,
    )


async def finish_execution(session, scope, failed):
    await scope.drain()
    if scope.execution is None:
        return
    scope.read_only = True
    state, output = "recovery_required", None
    if not failed:
        native = await session._saver.aget_tuple(scope.config)
        output = current_id(native)
        snapshot = await read_snapshot(scope)
        marker = (
            native.metadata.get("openeight_checkpoint") if native is not None else None
        )
        if not output or marker != {
            "configuration_sha256": scope.configuration_sha256,
            "graph_revision": scope.binding.graph_revision,
            "model_variant": scope.model_variant,
        }:
            invalid()
        if snapshot.interrupts and await qualified_interrupt(session, scope):
            state = "native_interrupt"
        elif quiescent(snapshot, native):
            state = "settled"
    final = replace(
        scope.execution,
        state=state,
        output_checkpoint_id=output,
        finished_at=utc_timestamp(),
    )
    await scope.assert_held()
    await application_call(lambda: scope.claim.finish(final))
    if await application_call(scope.claim.last_execution) != final:
        invalid()
    if state == "recovery_required" and not failed:
        invalid()


@asynccontextmanager
async def claimed_execution(session, value, config, context):
    owner = session._require()
    retained = asyncio.get_running_loop().create_future()
    with owner.lock:
        owner._ensure_open()
        session._operation_tasks.add(retained)
    try:
        async with _claimed_execution(session, value, config, context):
            yield
    finally:
        retained.set_result(None)
        session._operation_tasks.discard(retained)


@asynccontextmanager
async def _claimed_execution(session, value, config, context):
    from ._session import settle

    manager, failed = None, False
    try:
        manager = session._checkpoints.claim(config=config or {}, context=context)
        if not callable(manager.__aenter__) or not callable(manager.__aexit__):
            failed = True
    except Exception:
        failed = True
    if failed:
        invalid()
    claim = await application_call(manager.__aenter__)
    token, scope, primary = None, None, None
    try:
        scope, enrollment, normalized = await application_call(
            lambda: prepare_claim(session, claim, value, config)
        )
        token = CURRENT_CHECKPOINT_CLAIM.set(scope)
        await application_call(
            lambda: validate_history(session, scope, enrollment, value, normalized)
        )
        yield
    except BaseException as error:
        primary = error
        raise
    finally:
        from ._checkpoint_task import begin_settlement

        begin_settlement()
        cleanup_error = None
        try:
            if scope is not None and token is not None:
                await settle(
                    asyncio.create_task(
                        finish_execution(session, scope, primary is not None)
                    )
                )
        except BaseException as error:
            cleanup_error = error
        finally:
            try:
                if scope is not None:
                    await settle(asyncio.create_task(scope.drain()))
            except BaseException as error:
                cleanup_error = cleanup_error or error
            finally:
                if scope is not None:
                    scope.active = False
                if token is not None:
                    CURRENT_CHECKPOINT_CLAIM.reset(token)
                try:
                    await application_call(lambda: manager.__aexit__(None, None, None))
                except BaseException as error:
                    cleanup_error = cleanup_error or error
                finally:
                    if scope is not None and hasattr(scope, "invocation"):
                        scope.invocation.finish_execution()
        if primary is None and cleanup_error is not None:
            raise cleanup_error
