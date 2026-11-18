"""Deployment-bound command, attempt and immutable evidence transactions."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, replace, dataclass
import json
from uuid import uuid4, uuid5, NAMESPACE_URL

from openeight.langchain.durable import (
    DurableAttempt,
    DurableCommand,
    DurableObserveRecord,
    DurableThread,
)
from openeight.telemetry import uuid7
from journal_schema import RECOVERY_SQL
from journal_validation import (
    validate_uuid, parse_canonical_json, validate_attempt_settlement,
)


from local import (
    LocalError,
    Locks,
    canonical,
    connection,
    install_deployment,
    now,
    protected_directory,
    read_deployment,
    transaction,
)


@dataclass(frozen=True)
class LocalThread:
    deployment_id: str
    id: str
    case_id: str
    run_id: None
    run_started_at: None
    created_at: str
    updated_at: str
    status: str = "active"
    checkpoint_id: str | None = None
    run_ended_at: None = None
    revision: int = 0
    model_calls: int = 0
    read_calls: int = 0
    active_command_id: str | None = None
    observation_incomplete: int = 0


@dataclass(frozen=True)
class LocalAttempt:
    deployment_id: str
    id: str
    thread_id: str
    tool_call_id: str
    tool_name: str
    native_call_json: str
    created_at: str
    updated_at: str
    idempotency_key: str | None = None
    status: str = "prepared"
    result_json: str | None = None
    reason_code: str | None = None
    revision: int = 0


def _insert(db, table, record):
    data = asdict(record)
    db.execute(
        f"INSERT INTO {table} ({','.join(data)}) VALUES ({','.join('?' for _ in data)})",
        tuple(data.values()),
    )


def mutable_json(value):
    """Project immutable catalog schemas to JSON for the application journal."""
    from collections.abc import Mapping

    if isinstance(value, Mapping):
        return {key: mutable_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [mutable_json(item) for item in value]
    return value


def lifecycle_name(name, suffix):
    """Match the public Event name limit while retaining the lifecycle suffix."""
    return name.encode("utf-8")[:128 - len(suffix)].decode("utf-8", errors="ignore") + suffix


class SQLiteExecutionStore:
    """Application-owned journal implementing the public DurableExecutionStore protocol."""

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
                raise LocalError("revision_conflict")
            changes = {}
            if update.status is not None:
                if update.status not in {
                    "active",
                    "waiting",
                    "waiting_customer",
                    "reconciliation_required",
                }:
                    raise LocalError("invalid_transition")
                changes["status"] = update.status
            if update.checkpoint_id is not None:
                changes["checkpoint_id"] = update.checkpoint_id
            if (
                type(update.model_call_increment) is not int
                or update.model_call_increment < 0
            ):
                raise LocalError("invalid_transition")
            if update.model_call_increment:
                changes["model_calls"] = (
                    thread.model_calls + update.model_call_increment
                )
            if (
                type(update.read_call_increment) is not int
                or update.read_call_increment < 0
            ):
                raise LocalError("invalid_transition")
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
                raise LocalError("revision_conflict")
            changes = {"status": update.status, "reason_code": update.reason_code}
            event = update.event
            if event is not None:
                paused = update.status == "waiting" and record.status != "waiting"
                resumed = update.status == "executing" and record.status == "waiting"
                if not paused and not resumed:
                    raise LocalError("invalid_transition_event")
                column = "pause_event_json" if paused else "resume_event_json"
                payload = parse_canonical_json(event.payload_json)
                template = parse_canonical_json(record.event_template_json)

                expected = {
                    "id": event.id,
                    "run_id": template["run_id"],
                    "decision_id": record.decision_id,
                    "tool_id": record.tool_id,
                    "tool_version_id": record.tool_version_id,
                    "tool_name": record.tool_name,
                    "name": lifecycle_name(record.tool_name, ".paused" if paused else ".resumed"),
                    "observation_kind": "lifecycle",
                    "operation_kind": "unspecified",
                    "outcome": "interrupted" if paused else "unspecified",
                    "occurred_at": event.created_at,
                    "attributes_json": "{}",
                }
                if template.get("parent_event_id"):
                    expected["parent_event_id"] = template["parent_event_id"]
                validate_uuid(event.id)
                if (
                    event.thread_id != thread_id
                    or event.kind != "event"
                    or payload != expected
                ):
                    raise LocalError("invalid_transition_event")
                previous = getattr(record, column)
                if previous is not None and previous != event.payload_json:
                    raise LocalError("transition_event_conflict")
                changes[column] = event.payload_json
                self._record(db, event)
            result = self._update(db, "attempts", "decision_id", record, changes)
            self.fault("after_attempt_transition")
            return result

    def settle_attempt(self, thread_id, decision_id, expected_revision, settlement):
        self._locked(thread_id)
        parse_canonical_json(settlement.result_json)
        if settlement.status not in {"succeeded", "failed", "blocked"}:
            raise LocalError("invalid_settlement")
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
                    raise LocalError("settlement_conflict")
                return record
            if record.revision != expected_revision:
                raise LocalError("revision_conflict")
            validate_attempt_settlement(record, settlement, thread_id)
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
        payload = parse_canonical_json(record.payload_json, 1044480)
        if (
            record.kind != "event"
            or record.thread_id != thread_id
            or payload.get("decision_id") is not None
            or payload.get("observation_kind") not in {"llm_call", "span", "tool_call"}
        ):
            raise LocalError("invalid_callback_event")
        with self._db() as db, transaction(db):
            thread = self._thread(db, thread_id)
            if thread.run_ended_at:
                raise LocalError("thread_terminal")
            self._record(db, record)

    def _recovery_chain(self, db, command):
        chain, seen = [], {command.id}
        while command.recover_command_id:
            command = self._command(db, command.thread_id, command.recover_command_id)
            if command.id in seen:
                raise LocalError("command_conflict")
            seen.add(command.id)
            chain.append(command)
        return chain


    def __init__(self, state, deployment, *, fault=None):
        self.state = protected_directory(state)
        self.path = self.state / "recovery.sqlite"
        self.deployment = deployment
        self.locks = Locks(state)
        self.held = ContextVar("customer_support_lock", default=None)
        self.fault = fault or (lambda point: None)

    def initialize(self):
        self.deployment.validate()
        with connection(self.path) as db:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='deployment'"
            ).fetchone():
                db.executescript(RECOVERY_SQL)
            attempt_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(attempts)")
            }
            catalog_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(catalog_versions)")
            }
            if (
                not {
                    "request_format",
                    "policy_contract_set_digest",
                    "decision_input_json",
                }
                <= attempt_columns
                or "tool_contract_digest" not in catalog_columns
            ):
                raise LocalError("durable_request_format_unsupported")
            if db.execute(
                "SELECT 1 FROM attempts WHERE request_format != 'decision-request-v3' LIMIT 1"
            ).fetchone():
                raise LocalError("durable_request_format_unsupported")
            with transaction(db):
                install_deployment(db, self.deployment)

    @contextmanager
    def acquire(self, thread_id):
        with self.locks.thread(thread_id):
            token = self.held.set(thread_id)
            try:
                yield
            finally:
                self.held.reset(token)

    def _locked(self, thread_id):
        if self.held.get() != thread_id:
            raise LocalError("execution_not_owned")

    @contextmanager
    def _db(self):
        with connection(self.path) as db:
            if read_deployment(db) != self.deployment:
                raise LocalError("deployment_mismatch")
            yield db

    def _thread(self, db, thread_id):
        row = db.execute(
            "SELECT * FROM threads WHERE deployment_id=? AND id=?",
            (self.deployment.id, thread_id),
        ).fetchone()
        if row is None:
            raise LocalError("not_found")
        return self._thread_value(row)

    def load(self, thread_id):
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM threads WHERE deployment_id=? AND id=?",
                (self.deployment.id, thread_id),
            ).fetchone()
            return self._thread_value(row) if row else None

    def _mode(self, mode):
        if self.deployment.mode != mode:
            raise LocalError("execution_mode_mismatch")

    def _thread_value(self, row):
        if self.deployment.mode == "standalone":
            if any(
                row[k] is not None for k in ("run_id", "run_started_at", "run_ended_at")
            ):
                raise LocalError("execution_mode_mismatch")
            return LocalThread(**dict(row))
        if not row["run_id"] or not row["run_started_at"]:
            raise LocalError("execution_mode_mismatch")
        return DurableThread(**dict(row))

    def _command(self, db, thread_id, command_id):
        row = db.execute(
            "SELECT * FROM ui_commands WHERE deployment_id=? AND thread_id=? AND id=?",
            (self.deployment.id, thread_id, command_id),
        ).fetchone()
        if row is None:
            raise LocalError("not_found")
        return DurableCommand(**dict(row))

    def prior_customer_inputs(self, thread_id, command_id):
        with self._db() as db:
            self._thread(db, thread_id)
            command = self._command(db, thread_id, command_id)
            rows = db.execute(
                "SELECT id,request_json FROM ui_commands WHERE deployment_id=? AND thread_id=? "
                "AND reason_code='integration_unavailable' AND status='completed' AND input_recorded=0 "
                "AND rowid < (SELECT rowid FROM ui_commands WHERE deployment_id=? AND id=?) ORDER BY rowid",
                (self.deployment.id, thread_id, self.deployment.id, command.id),
            ).fetchall()
            return tuple(
                (row["id"], parse_canonical_json(row["request_json"])["message"]) for row in rows
            )

    def open_case(self, case_id):
        with self._db() as db:
            row = db.execute(
                "SELECT id FROM threads WHERE deployment_id=? AND case_id=? AND status!='completed'",
                (self.deployment.id, case_id),
            ).fetchone()
            return row["id"] if row else None

    def find_command(self, command_id):
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM ui_commands WHERE deployment_id=? AND id=?",
                (self.deployment.id, command_id),
            ).fetchone()
            return DurableCommand(**dict(row)) if row else None

    def _record(self, db, record):
        self._mode("integrated")
        thread = self._thread(db, record.thread_id)
        payload = parse_canonical_json(record.payload_json, 1044480)
        if (
            record.kind not in {"run_active", "event", "run_complete"}
            or record.status != "pending"
        ):
            raise LocalError("invalid_outbox")
        if payload.get("id") != (
            thread.run_id if record.kind != "event" else record.id
        ):
            raise LocalError("invalid_outbox")
        if record.kind == "event" and payload.get("run_id") != thread.run_id:
            raise LocalError("invalid_outbox")
        previous = db.execute(
            "SELECT * FROM outbox WHERE deployment_id=? AND id=?",
            (self.deployment.id, record.id),
        ).fetchone()
        data = {"deployment_id": self.deployment.id, **asdict(record)}
        if previous:
            if any(previous[k] != v for k, v in data.items() if k != "status"):
                raise LocalError("outbox_conflict")
            return
        db.execute(
            "INSERT INTO outbox (deployment_id,id,thread_id,kind,payload_json,status,created_at) VALUES (?,?,?,?,?,?,?)",
            (
                self.deployment.id,
                record.id,
                record.thread_id,
                record.kind,
                record.payload_json,
                record.status,
                record.created_at,
            ),
        )

    def _create(self, db, thread, active):
        self._mode("integrated")
        if (
            thread.deployment_id != self.deployment.id
            or thread.revision
            or thread.run_ended_at
            or thread.status != "active"
            or active.kind != "run_active"
            or active.thread_id != thread.id
        ):
            raise LocalError("invalid_thread")
        validate_uuid(thread.id)
        validate_uuid(thread.run_id)
        _insert(db, "threads", thread)
        self._record(db, active)

    def admit(self, request, *, case_id=None):
        """Application command ingress; graph settlement is SDK-owned."""
        if type(request) is not dict or request.get("kind") not in {
            "start",
            "reply",
            "resume",
        }:
            raise LocalError("invalid_input")
        kind = request["kind"]
        keys = {"kind", "command_id"} | (
            {"case_id", "message"}
            if kind == "start"
            else {"thread_id", "message"}
            if kind == "reply"
            else {"thread_id"}
        )
        if set(request) != keys:
            raise LocalError("invalid_input")
        command_id = request["command_id"]
        validate_uuid(command_id)
        if kind != "resume":
            if (
                type(request["message"]) is not str
                or not request["message"].strip()
                or len(request["message"].encode()) > 8192
            ):
                raise LocalError("invalid_input")
        snapshot = canonical(request)
        previous = self.find_command(command_id)
        if previous:
            if previous.request_json != snapshot:
                raise LocalError("command_conflict")
            return previous, False
        thread_id = (
            str(uuid5(NAMESPACE_URL, self.deployment.id + "/" + command_id))
            if kind == "start"
            else request["thread_id"]
        )
        with self.acquire(thread_id), self._db() as db, transaction(db):
            # Recheck under ownership; the fast retry path is intentionally read-only.
            row = db.execute(
                "SELECT * FROM ui_commands WHERE deployment_id=? AND id=?",
                (self.deployment.id, command_id),
            ).fetchone()
            if row:
                if row["request_json"] != snapshot:
                    raise LocalError("command_conflict")
                return DurableCommand(**dict(row)), False
            if db.execute(
                "SELECT 1 FROM ui_commands WHERE deployment_id=? AND thread_id=? AND status='accepted'",
                (self.deployment.id, thread_id),
            ).fetchone():
                raise LocalError("thread_busy")
            ts = now()
            recovery = None
            if kind == "start":
                if case_id != request["case_id"] or not case_id:
                    raise LocalError("not_found")
                if db.execute(
                    "SELECT 1 FROM threads WHERE deployment_id=? AND case_id=? AND status!='completed'",
                    (self.deployment.id, case_id),
                ).fetchone():
                    raise LocalError("case_busy")
                if self.deployment.mode == "standalone":
                    thread = LocalThread(
                        self.deployment.id, thread_id, case_id, None, None, ts, ts
                    )
                    _insert(db, "threads", thread)
                else:
                    thread = DurableThread(
                        self.deployment.id, thread_id, case_id, uuid7(), ts, ts, ts
                    )
                    payload = {
                        "id": thread.run_id,
                        "started_at": ts,
                        "status": "active",
                        "attributes_json": "{}",
                    }
                    active = DurableObserveRecord(
                        uuid7(), thread_id, "run_active", canonical(payload), ts
                    )
                    self._create(db, thread, active)
            else:
                thread = self._thread(db, thread_id)
                if (
                    thread.status == "completed"
                    and not (kind == "resume" and thread.active_command_id)
                ) or (thread.run_ended_at is not None and kind != "resume"):
                    raise LocalError("thread_terminal")
                if thread.active_command_id:
                    active = self._command(db, thread_id, thread.active_command_id)
                    if kind != "resume" or active.status not in {
                        "running",
                        "reconciliation_required",
                    }:
                        raise LocalError("thread_busy")
                    recovery = active.id
                elif kind == "reply" and thread.status != "waiting_customer":
                    raise LocalError("command_conflict")
                elif kind == "resume" and thread.status not in {
                    "waiting",
                    "reconciliation_required",
                }:
                    raise LocalError("command_conflict")
            command = DurableCommand(
                self.deployment.id,
                command_id,
                thread_id,
                kind,
                snapshot,
                ts,
                ts,
                recover_command_id=recovery,
            )
            _insert(db, "ui_commands", command)
            if kind != "resume":
                db.execute(
                    "INSERT INTO conversation_turns VALUES (?,?,?,?,?,?,?,?)",
                    (
                        self.deployment.id,
                        str(uuid4()),
                        thread_id,
                        command_id,
                        0,
                        "customer",
                        request["message"],
                        ts,
                    ),
                )
            self.fault("before_command_commit")
        self.fault("after_command_commit")
        return command, True

    def _update(self, db, table, key, record, changes):
        revision = record.revision
        changes = {**changes, "revision": revision + 1, "updated_at": now()}
        assignments = ",".join(k + "=?" for k in changes)
        result = db.execute(
            f"UPDATE {table} SET {assignments} WHERE deployment_id=? AND {key}=? AND revision=?",
            (*changes.values(), self.deployment.id, getattr(record, key), revision),
        )
        if result.rowcount != 1:
            raise LocalError("revision_conflict")
        return replace(record, **changes)

    def begin_command(
        self, thread_id, command_id, expected_revision, expected_thread_revision
    ):
        self._locked(thread_id)
        with self._db() as db, transaction(db):
            thread = self._thread(db, thread_id)
            command = self._command(db, thread_id, command_id)
            if (
                command.revision != expected_revision
                or thread.revision != expected_thread_revision
                or command.status != "accepted"
                or (thread.run_ended_at and command.kind != "resume")
            ):
                raise LocalError("revision_conflict")
            if thread.active_command_id:
                original = self._command(db, thread_id, thread.active_command_id)
                if command.recover_command_id != original.id or original.status not in {
                    "running",
                    "reconciliation_required",
                }:
                    raise LocalError("thread_busy")
                self._update(
                    db,
                    "ui_commands",
                    "id",
                    original,
                    {"status": "waiting", "reason_code": "interrupted"},
                )
            elif command.recover_command_id:
                raise LocalError("command_conflict")
            self._update(
                db,
                "threads",
                "id",
                thread,
                {
                    "active_command_id": command.id,
                    "status": thread.status if thread.run_ended_at else "active",
                    "observation_incomplete": int(
                        bool(
                            thread.observation_incomplete or command.recover_command_id
                        )
                    ),
                },
            )
            return self._update(db, "ui_commands", "id", command, {"status": "running"})

    def refuse_dispatch(self, command_id):
        """Make a pre-execution refusal visible without retrying it automatically."""
        command = self.find_command(command_id)
        if command is None or command.status != "accepted":
            return
        with self.acquire(command.thread_id), self._db() as db, transaction(db):
            command = self._command(db, command.thread_id, command_id)
            thread = self._thread(db, command.thread_id)
            if command.status != "accepted":
                return
            if thread.active_command_id:
                original = self._command(
                    db, command.thread_id, thread.active_command_id
                )
                if command.recover_command_id != original.id or original.status not in {
                    "running",
                    "reconciliation_required",
                }:
                    raise LocalError("command_conflict")
                self._update(
                    db,
                    "ui_commands",
                    "id",
                    original,
                    {"status": "waiting", "reason_code": "interrupted"},
                )
            self._update(
                db,
                "ui_commands",
                "id",
                command,
                {
                    "status": "reconciliation_required",
                    "reason_code": "execution_refused",
                },
            )
            self._update(
                db,
                "threads",
                "id",
                thread,
                {
                    "status": thread.status
                    if thread.run_ended_at
                    else "reconciliation_required",
                    "active_command_id": command.id,
                },
            )

    def record_command_input(
        self, thread_id, command_id, expected_revision, checkpoint_id
    ):
        self._locked(thread_id)
        with self._db() as db, transaction(db):
            command = self._command(db, thread_id, command_id)
            thread = self._thread(db, thread_id)
            if command.input_recorded:
                if command.input_checkpoint_id != checkpoint_id:
                    raise LocalError("command_conflict")
                return command
            current = (
                self._command(db, thread_id, thread.active_command_id)
                if thread.active_command_id
                else None
            )
            recovering = (
                current is not None
                and command_id in {c.id for c in self._recovery_chain(db, current)}
                and command.status == "waiting"
                and command.reason_code == "interrupted"
            )
            if command.revision != expected_revision or not (
                recovering
                or command.status == "running"
                and thread.active_command_id == command_id
            ):
                raise LocalError("revision_conflict")
            return self._update(
                db,
                "ui_commands",
                "id",
                command,
                {"input_recorded": 1, "input_checkpoint_id": checkpoint_id},
            )

    def _attempt(self, db, thread_id, decision_id):
        row = db.execute(
            "SELECT * FROM attempts WHERE deployment_id=? AND thread_id=? AND decision_id=?",
            (self.deployment.id, thread_id, decision_id),
        ).fetchone()
        if row is None:
            raise LocalError("not_found")
        return DurableAttempt(**dict(row))

    def get_attempt(self, thread_id, tool_call_id):
        with self._db() as db:
            self._thread(db, thread_id)
            row = db.execute(
                "SELECT * FROM attempts WHERE deployment_id=? AND thread_id=? AND tool_call_id=?",
                (self.deployment.id, thread_id, tool_call_id),
            ).fetchone()
            return DurableAttempt(**dict(row)) if row else None

    def attempts(self, thread_id):
        with self._db() as db:
            self._thread(db, thread_id)
            return tuple(
                DurableAttempt(**dict(row))
                for row in db.execute(
                    "SELECT * FROM attempts WHERE deployment_id=? AND thread_id=? ORDER BY created_at,decision_id",
                    (self.deployment.id, thread_id),
                )
            )

    def prepare(self, attempt):
        self._locked(attempt.thread_id)
        self._mode("integrated")
        if (
            attempt.deployment_id != self.deployment.id
            or attempt.status != "prepared"
            or attempt.revision
            or attempt.result_json
            or attempt.pause_event_json
            or attempt.resume_event_json
            or attempt.terminal_event_json
        ):
            raise LocalError("invalid_attempt")
        native = parse_canonical_json(attempt.native_call_json)
        selected = parse_canonical_json(attempt.decision_input_json)
        event = parse_canonical_json(attempt.event_template_json)
        if event.get("parent_event_id") is not None:
            validate_uuid(event["parent_event_id"])
        if (
            native.get("id") != attempt.tool_call_id
            or native.get("name") != attempt.tool_name
            or type(selected) is not dict
        ):
            raise LocalError("invalid_attempt")
        if attempt.idempotency_key is not None:
            validate_uuid(attempt.idempotency_key)
            if native["args"].get("idempotency_key") != attempt.idempotency_key:
                raise LocalError("invalid_attempt")
        with self._db() as db, transaction(db):
            thread = self._thread(db, attempt.thread_id)
            if not thread.active_command_id or thread.run_ended_at:
                raise LocalError("execution_not_owned")
            if (
                event.get("id") != attempt.event_id
                or event.get("run_id") != thread.run_id
                or event.get("decision_id") != attempt.decision_id
            ):
                raise LocalError("invalid_attempt")
            row = db.execute(
                "SELECT * FROM attempts WHERE deployment_id=? AND thread_id=? AND tool_call_id=?",
                (self.deployment.id, attempt.thread_id, attempt.tool_call_id),
            ).fetchone()
            if row:
                previous = DurableAttempt(**dict(row))
                mutable = {
                    "status",
                    "result_json",
                    "terminal_event_json",
                    "pause_event_json",
                    "resume_event_json",
                    "reason_code",
                    "revision",
                    "updated_at",
                }
                if any(
                    getattr(previous, key) != value
                    for key, value in asdict(attempt).items()
                    if key not in mutable
                ):
                    raise LocalError("attempt_conflict")
                return previous
            _insert(db, "attempts", attempt)
            return attempt

    def pending(self, thread_id):
        with self._db() as db:
            self._thread(db, thread_id)
            rows = db.execute(
                "SELECT id,thread_id,kind,payload_json,created_at,status FROM outbox WHERE deployment_id=? AND thread_id=? AND status=?",
                (self.deployment.id, thread_id, "pending"),
            ).fetchall()
            records = [DurableObserveRecord(**dict(row)) for row in rows]
            return tuple(
                sorted(
                    records,
                    key=lambda r: (
                        {"run_active": 0, "event": 1, "run_complete": 2}[r.kind],
                        json.loads(r.payload_json).get("occurred_at", r.created_at),
                        r.id,
                    ),
                )
            )

    def acknowledge(self, thread_id, record_id):
        self._locked(thread_id)
        with self._db() as db, transaction(db):
            thread = self._thread(db, thread_id)
            row = db.execute(
                "SELECT * FROM outbox WHERE deployment_id=? AND thread_id=? AND id=?",
                (self.deployment.id, thread_id, record_id),
            ).fetchone()
            if not row:
                raise LocalError("not_found")
            if row["status"] == "acknowledged":
                return
            db.execute(
                "UPDATE outbox SET status='acknowledged' WHERE deployment_id=? AND id=?",
                (self.deployment.id, record_id),
            )
            if row["kind"] == "run_complete":
                if not thread.run_ended_at:
                    raise LocalError("invalid_completion")
                self._update(db, "threads", "id", thread, {"status": "completed"})

    def begin_completion(self, thread_id, expected_revision, ended_at, record):
        self._locked(thread_id)
        with self._db() as db, transaction(db):
            thread = self._thread(db, thread_id)
            if thread.run_ended_at:
                if thread.run_ended_at != ended_at:
                    raise LocalError("completion_conflict")
                self._record(db, record)
                return thread
            if (
                thread.revision != expected_revision
                or record.kind != "run_complete"
                or record.thread_id != thread_id
            ):
                raise LocalError("revision_conflict")
            if db.execute(
                "SELECT 1 FROM outbox WHERE deployment_id=? AND thread_id=? AND status='pending'",
                (self.deployment.id, thread_id),
            ).fetchone():
                raise LocalError("evidence_pending")
            if db.execute(
                "SELECT 1 FROM attempts WHERE deployment_id=? AND thread_id=? AND status NOT IN ('succeeded','failed','blocked')",
                (self.deployment.id, thread_id),
            ).fetchone():
                raise LocalError("attempt_pending")
            payload = parse_canonical_json(record.payload_json)
            if payload != {
                "id": thread.run_id,
                "started_at": thread.run_started_at,
                "ended_at": ended_at,
                "status": "completed",
                "attributes_json": "{}",
            }:
                raise LocalError("invalid_completion")
            result = self._update(
                db, "threads", "id", thread, {"run_ended_at": ended_at}
            )
            self._record(db, record)
            return result

    def _turns(self, db, command, turns):
        for ordinal, body in enumerate(turns, 1):
            if type(body) is not str or len(body.encode()) > 8192:
                raise LocalError("invalid_turn")
            previous = db.execute(
                "SELECT body,speaker FROM conversation_turns WHERE deployment_id=? AND command_id=? AND ordinal=?",
                (self.deployment.id, command.id, ordinal),
            ).fetchone()
            if previous:
                if previous["body"] != body or previous["speaker"] != "agent":
                    raise LocalError("command_conflict")
                continue
            db.execute(
                "INSERT INTO conversation_turns VALUES (?,?,?,?,?,?,?,?)",
                (
                    self.deployment.id,
                    str(uuid4()),
                    command.thread_id,
                    command.id,
                    ordinal,
                    "agent",
                    body,
                    now(),
                ),
            )

    def settle_command(self, thread_id, command_id, expected_revision, settlement):
        self._locked(thread_id)
        if settlement.status not in {
            "waiting",
            "completed",
            "failed",
            "reconciliation_required",
        } or settlement.thread_status not in {
            "active",
            "waiting",
            "waiting_customer",
            "completed",
            "reconciliation_required",
        }:
            raise LocalError("invalid_settlement")
        with self._db() as db, transaction(db):
            thread = self._thread(db, thread_id)
            command = self._command(db, thread_id, command_id)
            if command.status != "running":
                if (
                    command.status,
                    command.output_checkpoint_id,
                    command.reason_code,
                ) != (
                    settlement.status,
                    settlement.output_checkpoint_id,
                    settlement.reason_code,
                ):
                    raise LocalError("command_conflict")
                previous_turns = tuple(
                    row["body"]
                    for row in db.execute(
                        "SELECT body FROM conversation_turns WHERE deployment_id=? AND command_id=? AND speaker='agent' ORDER BY ordinal",
                        (self.deployment.id, command.id),
                    )
                )
                if previous_turns != settlement.visible_turns:
                    raise LocalError("command_conflict")
                if command.recover_command_id:
                    root = self._recovery_chain(db, command)[-1]
                    if root.id != settlement.recovered_command_id:
                        raise LocalError("command_conflict")
                    previous_recovered = tuple(
                        row["body"]
                        for row in db.execute(
                            "SELECT body FROM conversation_turns WHERE deployment_id=? AND command_id=? AND speaker='agent' ORDER BY ordinal",
                            (self.deployment.id, root.id),
                        )
                    )
                    if previous_recovered != settlement.recovered_turns:
                        raise LocalError("command_conflict")
                return command
            if (
                command.revision != expected_revision
                or thread.revision != settlement.expected_thread_revision
                or thread.active_command_id != command_id
            ):
                raise LocalError("revision_conflict")
            table = (
                "attempts" if self.deployment.mode == "integrated" else "local_attempts"
            )
            unfinished = db.execute(
                f"SELECT status FROM {table} WHERE deployment_id=? AND thread_id=? AND status NOT IN ('succeeded','failed','blocked')",
                (self.deployment.id, thread_id),
            ).fetchall()
            if unfinished and settlement.thread_status not in {
                "waiting",
                "reconciliation_required",
            }:
                raise LocalError("attempt_pending")
            if (
                self.deployment.mode == "integrated"
                and settlement.thread_status == "completed"
                and thread.status != "completed"
            ):
                raise LocalError("evidence_pending")
            if command.recover_command_id:
                chain = self._recovery_chain(db, command)
                original = chain[-1]
                if (
                    original.id != settlement.recovered_command_id
                    or original.revision != settlement.recovered_command_revision
                    or original.status != "waiting"
                    or any(
                        c.status != "waiting" or c.reason_code != "interrupted"
                        for c in chain
                    )
                    or settlement.recovered_status
                    not in {"waiting", "completed", "failed", "reconciliation_required"}
                ):
                    raise LocalError("command_conflict")
                self._turns(db, original, settlement.recovered_turns)
                for recovered in chain:
                    self._update(
                        db,
                        "ui_commands",
                        "id",
                        recovered,
                        {
                            "status": settlement.recovered_status,
                            "output_checkpoint_id": settlement.output_checkpoint_id,
                        },
                    )
            elif settlement.recovered_command_id is not None:
                raise LocalError("command_conflict")
            self._turns(db, command, settlement.visible_turns)
            result = self._update(
                db,
                "ui_commands",
                "id",
                command,
                {
                    "status": settlement.status,
                    "output_checkpoint_id": settlement.output_checkpoint_id,
                    "reason_code": settlement.reason_code,
                },
            )
            self._update(
                db,
                "threads",
                "id",
                thread,
                {
                    "status": settlement.thread_status,
                    "checkpoint_id": settlement.output_checkpoint_id,
                    "active_command_id": None,
                },
            )
            self.fault("before_command_settlement_commit")
            return result

    def transcript(self, thread_id):
        with self._db() as db:
            self._thread(db, thread_id)
            return tuple(
                dict(row)
                for row in db.execute(
                    "SELECT speaker,body,command_id,ordinal FROM conversation_turns WHERE deployment_id=? AND thread_id=? ORDER BY created_at,id",
                    (self.deployment.id, thread_id),
                )
            )

    def commands_for_dispatch(self):
        with self._db() as db:
            return tuple(
                DurableCommand(**dict(row))
                for row in db.execute(
                    "SELECT * FROM ui_commands WHERE deployment_id=? AND status='accepted' ORDER BY created_at,id",
                    (self.deployment.id,),
                )
            )

    def record_catalog(self, facts, results):
        self._mode("integrated")

        with self.locks.hold("catalog"), self._db() as db, transaction(db):
            for result in results:
                if result.tool is None or result.tool_version is None:
                    raise LocalError("tool_catalog_unavailable")
                fact = facts[result.request_index]
                version = result.tool_version
                schema = canonical(mutable_json(fact.input_schema))
                rows = db.execute(
                    "SELECT * FROM catalog_versions WHERE deployment_id=? AND tool_name=?",
                    (self.deployment.id, fact.name),
                ).fetchall()
                if any(row["schema_json"] != schema for row in rows):
                    raise LocalError("tool_schema_changed")
                row = next(
                    (row for row in rows if row["tool_version_id"] == version.id), None
                )
                if row:
                    if row["tool_id"] != result.tool.id:
                        raise LocalError("tool_catalog_conflict")
                    # Policy contracts can change while schema identity stays fixed.
                    db.execute(
                        "UPDATE catalog_versions SET tool_contract_digest=?, observed_at=? "
                        "WHERE deployment_id=? AND tool_name=? AND tool_version_id=?",
                        (
                            version.tool_contract_digest,
                            now(),
                            self.deployment.id,
                            fact.name,
                            version.id,
                        ),
                    )
                    continue
                db.execute(
                    "INSERT INTO catalog_versions VALUES (?,?,?,?,?,?,?)",
                    (
                        self.deployment.id,
                        fact.name,
                        schema,
                        result.tool.id,
                        version.id,
                        version.tool_contract_digest,
                        now(),
                    ),
                )

    def local_attempts(self, thread_id):
        with self._db() as db:
            self._thread(db, thread_id)
            return tuple(
                LocalAttempt(**dict(r))
                for r in db.execute(
                    "SELECT * FROM local_attempts WHERE deployment_id=? AND thread_id=? ORDER BY created_at,id",
                    (self.deployment.id, thread_id),
                )
            )

    def local_prepare(self, attempt):
        self._mode("standalone")
        self._locked(attempt.thread_id)
        validate_uuid(attempt.id)
        call = parse_canonical_json(attempt.native_call_json)
        if (
            attempt.deployment_id != self.deployment.id
            or attempt.status != "prepared"
            or attempt.revision
            or call.get("id") != attempt.tool_call_id
            or call.get("name") != attempt.tool_name
        ):
            raise LocalError("invalid_attempt")
        from retailer import KINDS, validate_arguments

        arguments = dict(call.get("args", {}))
        key = arguments.pop("idempotency_key", None)
        if (attempt.tool_name in KINDS) != (
            key is not None
        ) or key != attempt.idempotency_key:
            raise LocalError("invalid_attempt")
        if key:
            validate_uuid(key)
        validate_arguments(attempt.tool_name, arguments)
        with self._db() as db, transaction(db):
            thread = self._thread(db, attempt.thread_id)
            if not thread.active_command_id or thread.status == "completed":
                raise LocalError("execution_not_owned")
            row = db.execute(
                "SELECT * FROM local_attempts WHERE deployment_id=? AND thread_id=? AND tool_call_id=?",
                (self.deployment.id, attempt.thread_id, attempt.tool_call_id),
            ).fetchone()
            if row:
                previous = LocalAttempt(**dict(row))
                mutable = {
                    "status",
                    "result_json",
                    "reason_code",
                    "revision",
                    "updated_at",
                }
                if any(
                    getattr(previous, k) != v
                    for k, v in asdict(attempt).items()
                    if k not in mutable
                ):
                    raise LocalError("attempt_conflict")
                return previous
            _insert(db, "local_attempts", attempt)
            return attempt

    def local_update(
        self, thread_id, attempt_id, expected_revision, status, result=None, reason=None
    ):
        self._mode("standalone")
        self._locked(thread_id)
        with self._db() as db, transaction(db):
            self._thread(db, thread_id)
            row = db.execute(
                "SELECT * FROM local_attempts WHERE deployment_id=? AND thread_id=? AND id=?",
                (self.deployment.id, thread_id, attempt_id),
            ).fetchone()
            if not row:
                raise LocalError("not_found")
            record = LocalAttempt(**dict(row))
            if record.status in {"succeeded", "failed"}:
                if (record.status, record.result_json, record.reason_code) != (
                    status,
                    result,
                    reason,
                ):
                    raise LocalError("settlement_conflict")
                return record
            if record.revision != expected_revision or status not in {
                "executing",
                "succeeded",
                "failed",
                "reconciliation_required",
            }:
                raise LocalError("revision_conflict")
            if status in {"succeeded", "failed"}:
                if record.status != "executing":
                    raise LocalError("invalid_settlement")
                parse_canonical_json(result)
            updated = self._update(
                db,
                "local_attempts",
                "id",
                record,
                {"status": status, "result_json": result, "reason_code": reason},
            )
            if status in {"succeeded", "failed"}:
                self.fault("after_local_attempt_result")
            return updated

    def unavailable(self, command_id):
        command = self.find_command(command_id)
        if (
            command is None
            or command.status not in {"accepted", "running"}
            or command.kind == "resume"
        ):
            return False
        with self.acquire(command.thread_id), self._db() as db, transaction(db):
            thread = self._thread(db, command.thread_id)
            command = self._command(db, command.thread_id, command.id)
            if (
                command.status not in {"accepted", "running"}
                or thread.active_command_id not in {None, command.id}
                or command.input_recorded
                or command.recover_command_id
                or any(
                    a.status not in {"succeeded", "failed", "blocked"}
                    for a in self.attempts(thread.id)
                )
            ):
                return False
            self._turns(
                db,
                command,
                (
                    "Support actions are unavailable while OpenEight integration is unavailable. Your message is saved; send a new message to retry when it is restored.",
                ),
            )
            self._update(
                db,
                "ui_commands",
                "id",
                command,
                {"status": "completed", "reason_code": "integration_unavailable"},
            )
            self._update(
                db,
                "threads",
                "id",
                thread,
                {"status": "waiting_customer", "active_command_id": None},
            )
            return True
