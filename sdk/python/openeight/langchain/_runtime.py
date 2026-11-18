"""Agent invocation ownership stays in process and never enters graph state."""

import threading
from contextlib import contextmanager
from contextvars import ContextVar

from langchain_core.runnables.config import var_child_runnable_config

from openeight._content_capture import capture_optional_content
from openeight._time import utc_timestamp
from openeight.content import TelemetryContentContext
from openeight.telemetry import uuid7

CURRENT_INVOCATION = ContextVar("openeight_invocation", default=None)
CURRENT_READ_CONTRACT = ContextVar("openeight_read_contract", default=None)
CURRENT_TOOL_ATTEMPT = ContextVar("openeight_attempt", default=None)


def tool_parent_event(invocation, request):
    config = getattr(getattr(request, "runtime", None), "config", None)
    if not isinstance(config, dict):
        return None
    callback_id = getattr(config.get("callbacks"), "parent_run_id", None)
    with invocation.lock:
        parent = invocation.entries.get(callback_id)
        return parent["event_id"] if parent is not None else None


@contextmanager
def invocation_context(invocation):
    """Bind SDK ownership while keeping the caller's runnable context isolated."""
    invocation_token = CURRENT_INVOCATION.set(invocation)
    # Native graphs merge this ambient config even when explicit callbacks are
    # supplied. Clear it while stepping the graph so outer SDK handlers stay out.
    runnable_token = var_child_runnable_config.set(None)
    try:
        yield
    finally:
        var_child_runnable_config.reset(runnable_token)
        CURRENT_INVOCATION.reset(invocation_token)


class AgentInvocation:
    """Own one Run, callback state, tool contracts and retained handler attempts."""

    def __init__(self, owner, operation):
        self.owner, self.operation = owner, operation
        self.content_policy = owner.content_policy
        self.catalog = owner.catalog
        self.token = object()
        self.run_id, self.started = uuid7(), utc_timestamp()
        self.lock = threading.RLock()
        self.entries, self.contracts = {}, {}
        self.event_jobs = []
        self.event_delivery_failed = False
        self.root_ended = self.completed = False
        self.callbacks_expired = False
        self.handlers = 0
        self.ended = None
        self.active = None
        self.root_charge = 512
        self.grace_timer = None
        self.catalog_generation = -1

    def capture_content(self, kind, direction, project):
        value, reason = capture_optional_content(
            self.content_policy,
            TelemetryContentContext(kind=kind, direction=direction),
            project,
        )
        if reason is not None:
            self.owner.record_content_capture_failure(reason)
        return value

    def update_tool_contracts(self, registrations, *, generation=None):
        with self.lock:
            if generation is not None:
                if (
                    self.completed
                    or generation < self.catalog_generation
                    or generation == self.catalog_generation
                    and len(registrations) < len(self.contracts)
                ):
                    return
                self.catalog_generation = generation
            self.contracts = {
                result.tool.name: result
                for result in registrations
                if result.status in {"registered", "replayed"}
                and result.tool is not None
                and result.tool_version is not None
            }

    def start(self):
        """Enforcement needs current contracts; observation queues catalog independently."""
        if self.owner.control is not None and not self.owner.fresh_contracts:
            from ._catalog import report_catalog

            self.update_tool_contracts(
                report_catalog(
                    self.owner.catalog_delivery.publisher, self.owner.catalog.facts
                )
            )
        else:
            with self.owner.lock:
                generation, snapshot = self.owner.catalog_delivery.versioned_snapshot()
                if not self.owner.fresh_contracts:
                    self.owner.catalog_delivery.request_refresh()
            self.update_tool_contracts(snapshot, generation=generation)
        self.active = self.owner.worker.enqueue(
            "active", self.run_id, self.build_run_payload()
        )

    def build_run_payload(self, complete=False):
        body = {
            "id": self.run_id,
            "started_at": self.started,
            "status": "active",
            "attributes_json": "{}",
        }
        if complete:
            body["ended_at"] = self.ended
            body["status"] = "completed"
        return body

    def enqueue_event(self, event_id, event):
        payload = event._as_write()
        for field in ("input_json", "output_json"):
            if payload.get(field) == "null":
                payload.pop(field, None)
        payload.update(id=event_id, run_id=self.run_id)
        with self.lock:
            job = self.owner.worker.enqueue(
                "event", self.run_id, payload, (self.active,)
            )
            self.event_delivery_failed = self.event_delivery_failed or any(
                item.failed for item in self.event_jobs
            )
            self.event_jobs = [
                item
                for item in self.event_jobs
                if not item.acknowledged and not item.failed
            ]
            if job is not None:
                self.event_jobs.append(job)

    def complete_when_settled(self):
        """Complete after execution and handlers end and callbacks settle or expire."""
        with self.lock:
            if not self.root_ended or self.handlers or self.completed:
                return
            unfinished = [
                entry
                for entry in self.entries.values()
                if not entry["terminal"] or entry.get("pending_captures")
            ]
            if unfinished and self.grace_timer is None:
                self.grace_timer = threading.Timer(
                    self.owner.delivery.late_callback_grace, self.expire_callbacks
                )
                self.grace_timer.daemon = True
                self.grace_timer.start()
                return
            if unfinished:
                return
            # Completion depends on active/Event delivery, never catalog success.
            self.completed = True
            self.owner.worker.enqueue(
                "complete",
                self.run_id,
                self.build_run_payload(True),
                (
                    self.active,
                    *self.event_jobs,
                    *((None,) if self.event_delivery_failed else ()),
                ),
            )
            for entry in self.entries.values():
                self.owner.worker.release(entry["charge"])
            self.entries.clear()
            self.owner.release(self)

    def expire_callbacks(self):
        """Drop callbacks that outlive the configured grace period."""
        with self.lock:
            self.callbacks_expired = True
            for entry in self.entries.values():
                if (
                    not entry["terminal"]
                    or entry.get("pending_captures")
                    or entry.get("placeholder")
                ):
                    entry["terminal"] = True
                    entry["expired"] = True
                    entry["pending_captures"] = {}
                    entry.pop("finish_event", None)
                    with self.owner.worker.condition:
                        self.owner.worker.dropped += 1
                        self.owner.worker.last_error = "callback_expired"
        self.complete_when_settled()

    def finish_execution(self):
        """Record root termination, then complete after retained work settles."""
        with self.lock:
            self.root_ended = True
            self.ended = utc_timestamp()
        self.complete_when_settled()
