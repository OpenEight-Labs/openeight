"""A delegation remains executing while its independently identified child waits."""

from langgraph._internal._constants import INTERRUPT, RESUME
from langgraph.pregel._task_status import read_task_statuses

from openeight.errors import ConfigurationError

from ._resume import resume_input, waiting_task


def _pending(task):
    status = read_task_statuses(task.pending_writes).get(task.task.id)
    return (
        task.task.error is None
        and (status is None or not status.finished and status.error is None)
        and all(channel in {INTERRUPT, RESUME} for _, channel, _ in task.pending_writes)
    )


def pending_delegations(snapshot, *, invocation):
    try:
        # Propagated interrupts must have valid leaf/ancestor ownership too.
        resume_input(snapshot, invocation=invocation)
        permitted = set()
        for attempt in invocation.store.attempts(invocation.thread_id):
            if attempt.status != "waiting":
                continue
            task, _, continuation = waiting_task(
                snapshot, invocation=invocation, attempt=attempt
            )
            if not _pending(task):
                raise ValueError
            key, seen = continuation.parent_attempt_key, set()
            while key is not None:
                if key in seen or len(seen) >= 16:
                    raise ValueError
                seen.add(key)
                parent = invocation.store.continuation(invocation.thread_id, key)
                parent_attempt = invocation.store.get_attempt(invocation.thread_id, key)
                candidates = [
                    t
                    for t in snapshot.native.tasks
                    if t.namespace == parent.checkpoint_namespace
                ]
                if (
                    parent.kind != "delegation"
                    or parent_attempt.status != "executing"
                    or len(candidates) != 1
                    or not _pending(candidates[0])
                ):
                    raise ValueError
                permitted.add(key)
                key = parent.parent_attempt_key
        return frozenset(permitted)
    except (ConfigurationError, ValueError, TypeError, KeyError, AttributeError):
        raise ConfigurationError("durable_reconciliation_required") from None
