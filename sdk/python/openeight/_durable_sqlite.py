"""Shared SQLite journal transitions for durable execution and telemetry."""

import json
from dataclasses import asdict
from uuid import UUID

from openeight._local_storage import LocalError, canonical, transaction


def _uuid(value, *, error=LocalError):
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise error("invalid_input") from None


def _json(value, maximum=65536, *, error=LocalError):
    try:
        parsed = json.loads(value)
        if (
            type(value) is not str
            or len(value.encode()) > maximum
            or canonical(parsed) != value
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise error("invalid_input") from None
    return parsed


def _telemetry_json(value, *, error=LocalError):
    """Validate optional telemetry independently of the native recovery result."""
    import math

    def validate(item, depth=0):
        if item is None or type(item) is bool:
            return
        if type(item) is int and -(1 << 63) <= item < (1 << 63):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is str:
            item.encode("utf-8")
            return
        if depth >= 32:
            raise ValueError
        if type(item) is list:
            for child in item:
                validate(child, depth + 1)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for key, child in item.items():
                validate(key, depth + 1)
                validate(child, depth + 1)
            return
        raise ValueError

    try:
        parsed = json.loads(value)
        validate(parsed)
        encoded = json.dumps(
            parsed,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        # Event JSON uses UTF-8, while the durable transport's outer JSON escapes it.
        if type(value) is not str or encoded != value or len(encoded.encode()) > 16384:
            raise ValueError
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise error("invalid_input") from None
    return parsed


def _insert(db, table, record):
    data = asdict(record)
    db.execute(
        f"INSERT INTO {table} ({','.join(data)}) VALUES ({','.join('?' for _ in data)})",
        tuple(data.values()),
    )


class JournalTransitions:
    _journal_error = LocalError

    def command(self, thread_id, command_id):
        with self._db() as db:
            self._thread(db, thread_id)
            return self._command(db, thread_id, command_id)

    def create(self, thread, active):
        self._locked(thread.id)
        with self._db() as db, transaction(db):
            self._create(db, thread, active)

    def transition(self, thread_id, expected_revision, update):
        self._locked(thread_id)
        with self._db() as db, transaction(db):
            thread = self._thread(db, thread_id)
            if (
                thread.revision != expected_revision
                or thread.status == "completed"
                or thread.run_ended_at
            ):
                raise self._journal_error("revision_conflict")
            changes = {}
            if update.status is not None:
                if update.status not in {
                    "active",
                    "waiting",
                    "waiting_customer",
                    "reconciliation_required",
                }:
                    raise self._journal_error("invalid_transition")
                changes["status"] = update.status
            if update.checkpoint_id is not None:
                changes["checkpoint_id"] = update.checkpoint_id
            if (
                type(update.model_call_increment) is not int
                or update.model_call_increment < 0
            ):
                raise self._journal_error("invalid_transition")
            if update.model_call_increment:
                changes["model_calls"] = (
                    thread.model_calls + update.model_call_increment
                )
            if (
                type(update.read_call_increment) is not int
                or update.read_call_increment < 0
            ):
                raise self._journal_error("invalid_transition")
            if update.read_call_increment:
                changes["read_calls"] = thread.read_calls + update.read_call_increment
            if update.observation_incomplete:
                changes["observation_incomplete"] = 1
            return self._update(db, "threads", "id", thread, changes)

    def transition_attempt(self, thread_id, decision_id, expected_revision, update):
        self._locked(thread_id)
        with self._db() as db, transaction(db):
            record = self._attempt(db, thread_id, decision_id)
            allowed = {
                "prepared": {"waiting", "executing", "reconciliation_required"},
                "waiting": {"waiting", "executing", "reconciliation_required"},
                "executing": {"executing", "reconciliation_required"},
                "reconciliation_required": {
                    "executing",
                    "waiting",
                    "reconciliation_required",
                },
            }
            if record.revision != expected_revision or update.status not in allowed.get(
                record.status, set()
            ):
                raise self._journal_error("revision_conflict")
            changes = {"status": update.status, "reason_code": update.reason_code}
            event = update.event
            if event is not None:
                paused = update.status == "waiting" and record.status != "waiting"
                resumed = update.status == "executing" and record.status == "waiting"
                if not paused and not resumed:
                    raise self._journal_error("invalid_transition_event")
                column = "pause_event_json" if paused else "resume_event_json"
                payload = _json(event.payload_json, error=self._journal_error)
                template = _json(record.event_template_json, error=self._journal_error)
                from openeight.langchain._callbacks import bounded_name

                expected = {
                    "id": event.id,
                    "run_id": template["run_id"],
                    "decision_id": record.decision_id,
                    "tool_id": record.tool_id,
                    "tool_version_id": record.tool_version_id,
                    "tool_name": record.tool_name,
                    "name": bounded_name(
                        record.tool_name, suffix=".paused" if paused else ".resumed"
                    ),
                    "observation_kind": "lifecycle",
                    "operation_kind": "unspecified",
                    "outcome": "interrupted" if paused else "unspecified",
                    "occurred_at": event.created_at,
                    "attributes_json": "{}",
                }
                if template.get("parent_event_id"):
                    expected["parent_event_id"] = template["parent_event_id"]
                _uuid(event.id, error=self._journal_error)
                if (
                    event.thread_id != thread_id
                    or event.kind != "event"
                    or payload != expected
                ):
                    raise self._journal_error("invalid_transition_event")
                previous = getattr(record, column)
                if previous is not None and previous != event.payload_json:
                    raise self._journal_error("transition_event_conflict")
                changes[column] = event.payload_json
                self._record(db, event)
            result = self._update(db, "attempts", "decision_id", record, changes)
            self.fault("after_attempt_transition")
            return result

    def settle_attempt(self, thread_id, decision_id, expected_revision, settlement):
        self._locked(thread_id)
        _json(settlement.result_json, error=self._journal_error)
        if settlement.status not in {"succeeded", "failed", "blocked"}:
            raise self._journal_error("invalid_settlement")
        with self._db() as db, transaction(db):
            record = self._attempt(db, thread_id, decision_id)
            event = settlement.event
            raw = event.payload_json if event else None
            if record.status in {"succeeded", "failed", "blocked"}:
                if (
                    record.status,
                    record.result_json,
                    record.reason_code,
                    record.terminal_event_json,
                ) != (
                    settlement.status,
                    settlement.result_json,
                    settlement.reason_code,
                    raw,
                ):
                    raise self._journal_error("settlement_conflict")
                return record
            if record.revision != expected_revision:
                raise self._journal_error("revision_conflict")
            native_result = _json(settlement.result_json, error=self._journal_error)
            if (
                type(native_result) is not dict
                or set(native_result) != {"content", "status"}
                or type(native_result["content"]) is not str
                or native_result["status"]
                != ("success" if settlement.status == "succeeded" else "error")
            ):
                raise self._journal_error("invalid_settlement")
            if settlement.status == "blocked":
                if event is not None or record.status == "executing":
                    raise self._journal_error("invalid_settlement")
            else:
                if (
                    event is None
                    or record.status != "executing"
                    or event.id != record.event_id
                    or event.thread_id != thread_id
                    or event.kind != "event"
                ):
                    raise self._journal_error("invalid_settlement")
                payload = _json(raw, error=self._journal_error)
                template = _json(record.event_template_json, error=self._journal_error)
                expected = {
                    **template,
                    "outcome": "success"
                    if settlement.status == "succeeded"
                    else "error",
                }
                expected.pop("output_json", None)
                if settlement.status == "succeeded" and "output_json" in payload:
                    try:
                        _telemetry_json(
                            payload["output_json"], error=self._journal_error
                        )
                    except self._journal_error:
                        raise self._journal_error("invalid_settlement") from None
                    expected["output_json"] = payload["output_json"]
                if settlement.status == "failed":
                    expected["error_json"] = canonical({"kind": "tool_error"})
                    expected.pop("output_json", None)
                from datetime import datetime

                try:
                    datetime.fromisoformat(
                        payload["occurred_at"].replace("Z", "+00:00")
                    )
                except (KeyError, TypeError, ValueError):
                    raise self._journal_error("invalid_settlement") from None
                expected["occurred_at"] = payload["occurred_at"]
                attributes = json.loads(payload.get("attributes_json", "{}"))
                if attributes.get("timing_source") == "receipt_recovery":
                    expected["attributes_json"] = canonical(
                        {
                            **json.loads(template.get("attributes_json", "{}")),
                            "timing_source": "receipt_recovery",
                        }
                    )
                elif "latency_ms" in payload:
                    if (
                        type(payload["latency_ms"]) is not int
                        or payload["latency_ms"] < 0
                    ):
                        raise self._journal_error("invalid_settlement")
                    expected["latency_ms"] = payload["latency_ms"]
                # Old frozen settlements without timing remain replayable.
                if payload != expected:
                    raise self._journal_error("invalid_settlement")
            result = self._update(
                db,
                "attempts",
                "decision_id",
                record,
                {
                    "status": settlement.status,
                    "result_json": settlement.result_json,
                    "reason_code": settlement.reason_code,
                    "terminal_event_json": raw,
                },
            )
            self.fault("after_attempt_result")
            if event:
                self._record(db, event)
            self.fault("after_attempt_outbox")
            return result

    def enqueue_callback_event(self, thread_id, record):
        self._locked(thread_id)
        payload = _json(record.payload_json, 1044480, error=self._journal_error)
        if (
            record.kind != "event"
            or record.thread_id != thread_id
            or payload.get("decision_id") is not None
            or payload.get("observation_kind") not in {"llm_call", "span", "tool_call"}
        ):
            raise self._journal_error("invalid_callback_event")
        with self._db() as db, transaction(db):
            thread = self._thread(db, thread_id)
            if thread.run_ended_at:
                raise self._journal_error("thread_terminal")
            self._record(db, record)

    def _recovery_chain(self, db, command):
        chain, seen = [], {command.id}
        while command.recover_command_id:
            command = self._command(db, command.thread_id, command.recover_command_id)
            if command.id in seen:
                raise self._journal_error("command_conflict")
            seen.add(command.id)
            chain.append(command)
        return chain
