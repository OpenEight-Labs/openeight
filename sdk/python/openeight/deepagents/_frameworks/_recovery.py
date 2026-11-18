"""Recover generated progress only from exact current native task evidence."""

from dataclasses import replace

from openeight._framework._types import CapturedGeneratedResult, DeepAgentsCompatibility
from openeight.errors import ConfigurationError
from openeight.langchain.durable import AttemptTransition

from .._continuation import frozen_native_identity, public_native_call
from .._deployment import canonical, parse
from ..durable import GeneratedStateReceipt
from ._replay import native_completion


async def recover_generated(
    compatibility: DeepAgentsCompatibility, invocation, attempt, continuation, snapshot
):
    frozen_native_identity(attempt, invocation=invocation, continuation=continuation)
    if continuation.kind not in {"state_write", "delegation"}:
        raise ConfigurationError("durable_attempt_conflict")
    receipt = invocation.store.generated_state_receipt(
        invocation.thread_id, attempt.tool_call_id
    )
    if receipt is not None:
        if attempt.status not in {"succeeded", "failed"}:
            return "reconciliation_required"
        replay = await compatibility.inspect_generated_replay(
            snapshot,
            invocation=invocation,
            attempt=attempt,
            continuation=continuation,
            receipt=receipt,
        )
        if replay.disposition == "unknown":
            return "reconciliation_required"
        invocation.generated_replays[attempt.tool_call_id] = replay
        return (
            "restored"
            if replay.disposition == "already_committed"
            else "resume_required"
        )
    if attempt.status not in {"executing", "reconciliation_required"}:
        return "reconciliation_required"
    tasks = [
        t
        for t in snapshot.native.tasks
        if t.namespace == continuation.checkpoint_namespace
    ]
    if len(tasks) != 1:
        return "reconciliation_required"
    task = tasks[0]
    observation = snapshot.native.observations.observations.get(task.namespace)
    if (
        observation is None
        or observation.native_call_id != continuation.native_tool_call_id
        or observation.identity.child_path != task.child_path
        or list(task.child_path) != parse(continuation.child_path_json)
        or task.call != public_native_call(attempt, continuation)
    ):
        return "reconciliation_required"
    disposition, result, update_json = native_completion(compatibility, task)
    receipt = GeneratedStateReceipt(
        attempt.deployment_id,
        attempt.thread_id,
        attempt.decision_id,
        1,
        task.input_checkpoint_id,
        update_json if update_json is not None else "{}",
    )
    if disposition == "pending":
        # This candidate is proof input only; no receipt is persisted for an absent update.
        replay = await compatibility.inspect_generated_replay(
            snapshot,
            invocation=invocation,
            attempt=attempt,
            continuation=continuation,
            receipt=receipt,
        )
        if replay.disposition != "replayable":
            return "reconciliation_required"
        return "resume_required"
    if disposition != "already_committed":
        return "reconciliation_required"
    proposed = replace(
        attempt,
        result_json=canonical({"content": result.content, "status": result.status}),
    )
    replay = await compatibility.inspect_generated_replay(
        snapshot,
        invocation=invocation,
        attempt=proposed,
        continuation=continuation,
        receipt=receipt,
    )
    if replay.disposition != "already_committed":
        return "reconciliation_required"
    if attempt.status != "executing":
        attempt = await invocation.storage_call(
            lambda: invocation.store.transition_attempt(
                invocation.thread_id,
                attempt.decision_id,
                attempt.revision,
                AttemptTransition("executing"),
            )
        )
    captured = CapturedGeneratedResult(result, receipt, None)
    await invocation.settle(
        attempt, compatibility.tool_message(result), recovered=True, generated=captured
    )
    invocation.generated_replays[attempt.tool_call_id] = replay
    return "restored"
