"""Single-deployment SQLite admission, continuations and atomic state receipts."""

import sqlite3
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from functools import lru_cache
from uuid import NAMESPACE_URL, uuid4, uuid5

from openeight.errors import ConfigurationError

from .. import _deployment
from .._continuation import DurableCallContext, durable_attempt_key
from .._frameworks._state import decode_state_update
from ..durable import DurableContinuation, DurableDeployment, GeneratedStateReceipt
from ._filesystem import (
    LocalError,
    Locks,
    canonical,
    connection,
    inspection_connection,
    now,
    protected_directory,
    transaction,
)
from ._journal import JournalTransitions, _insert, _json, _uuid
from ._schema import SCHEMA_SQL

TABLES = {
    "sdk_format",
    "deployments",
    "threads",
    "commands",
    "attempts",
    "outbox",
    "conversation_turns",
    "continuations",
    "generated_state_receipts",
    "command_results",
}


@lru_cache(maxsize=8)
def _expected_schema(schema_sql=SCHEMA_SQL):
    with closing(sqlite3.connect(":memory:")) as db:
        db.executescript(schema_sql)
        return _schema_identity(db)


def _schema_identity(db):
    return tuple(
        tuple(row)
        for row in db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
        )
    )


def command_request(*, kind, command_id, thread_id, message):
    if (
        kind not in {"start", "reply", "resume"}
        or type(command_id) is not str
        or not command_id
        or len(command_id.encode()) > 256
        or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in command_id)
    ):
        raise LocalError("command_conflict")
    if kind == "start":
        if thread_id is not None:
            raise LocalError("command_conflict")
    else:
        _uuid(thread_id)
    if kind == "resume":
        if message is not None:
            raise LocalError("command_conflict")
    elif (
        type(message) is not str or not message.strip() or len(message.encode()) > 8192
    ):
        raise LocalError("command_conflict")
    return canonical(
        {
            "kind": kind,
            "command_id": command_id,
            "thread_id": thread_id,
            "message": message,
        }
    )


class SQLiteDeepAgentsStore(JournalTransitions):
    _schema_sql = SCHEMA_SQL

    def __init__(self, directory, *, fault=None):
        self.state = protected_directory(directory)
        self.path = self.state / "execution.sqlite"
        self._inspect_existing()
        self.locks = Locks(self.state)
        self._deployment_id = None
        self._deployment_owned = ContextVar("openeight_deployment_lock", default=False)
        self.held = ContextVar("openeight_thread_lock", default=None)
        self._active_db = ContextVar("openeight_journal_transaction", default=None)
        self.fault = fault or (lambda point: None)
        with self.locks.hold("deployment"):
            self._inspect_existing()
            with connection(self.path, create=True) as db:
                tables = {
                    r[0]
                    for r in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if not tables:
                    db.executescript("BEGIN IMMEDIATE;" + self._schema_sql + "COMMIT;")
                self._check_schema(db)
                rows = db.execute("SELECT id FROM deployments").fetchall()
                if rows:
                    self._deployment_id = rows[0]["id"]

    def _inspect_existing(self):
        if not self.path.exists() and not self.path.is_symlink():
            return
        from ._filesystem import protected_file

        protected_file(self.path)
        try:
            with inspection_connection(self.path) as db:
                tables = {
                    r[0]
                    for r in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if tables and tables != TABLES:
                    raise LocalError("unsupported_deployment")
                if tables:
                    if _schema_identity(db) != _expected_schema(self._schema_sql):
                        raise LocalError("unsupported_deployment")
                    rows = db.execute(
                        "SELECT id,format_version FROM sdk_format"
                    ).fetchall()
                    if [tuple(row) for row in rows] != [(1, 1)]:
                        raise LocalError("unsupported_deployment")
        except sqlite3.Error:
            raise LocalError("unsupported_deployment") from None

    def _check_schema(self, db):
        tables = {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        rows = db.execute("SELECT id,format_version FROM sdk_format").fetchall()
        if (
            tables != TABLES
            or [tuple(r) for r in rows] != [(1, 1)]
            or _schema_identity(db) != _expected_schema(self._schema_sql)
        ):
            raise LocalError("unsupported_deployment")

    @contextmanager
    def acquire_deployment(self):
        if self._deployment_owned.get():
            raise LocalError("execution_busy")
        with self.locks.hold("deployment"):
            token = self._deployment_owned.set(True)
            try:
                yield
            finally:
                self._deployment_owned.reset(token)

    def _owned(self):
        if not self._deployment_owned.get():
            raise LocalError("execution_not_owned")

    @contextmanager
    def acquire(self, thread_id):
        self._owned()
        _uuid(thread_id)
        if self.held.get() is not None:
            raise LocalError("execution_busy")
        with self.locks.hold("thread-" + thread_id):
            token = self.held.set(thread_id)
            try:
                yield
            finally:
                self.held.reset(token)

    def _locked(self, thread_id):
        self._owned()
        if self.held.get() != thread_id:
            raise LocalError("execution_not_owned")

    @contextmanager
    def _db(self):
        active = self._active_db.get()
        if active is not None:
            yield active
            return
        with connection(self.path) as db:
            self._check_schema(db)
            token = self._active_db.set(db)
            try:
                yield db
            finally:
                self._active_db.reset(token)

    def _read_deployment(self, db):
        rows = db.execute("SELECT * FROM deployments").fetchall()
        if len(rows) != 1:
            raise ConfigurationError("durable_deployment_unbound")
        row = rows[0]
        _uuid(row["id"])
        _deployment.digest(row["credential_binding_sha256"])
        value = {key: row[key] for key in _deployment.IDENTITY_KEYS if key != "kind"}
        value["kind"] = row["identity_kind"]
        identity = _deployment.canonical(value)
        _deployment.identity(identity)
        if row["format_version"] != 1:
            raise LocalError("unsupported_deployment")
        if row["manifest_json"] is not None:
            saved = _deployment.manifest(row["manifest_json"])
            if (
                saved["deployment_id"] != row["id"]
                or _deployment.canonical(saved["identity"]) != identity
            ):
                raise LocalError("deployment_mismatch")
        self._deployment_id = row["id"]
        return DurableDeployment(
            row["id"],
            1,
            identity,
            row["credential_binding_sha256"],
            row["manifest_json"],
        )

    def deployment(self):
        with self._db() as db:
            value = self._read_deployment(db)
            if value.manifest_json is None:
                self._empty(db)
            return value

    def _empty(self, db):
        if any(
            db.execute("SELECT 1 FROM " + table + " LIMIT 1").fetchone()
            for table in (
                "threads",
                "commands",
                "attempts",
                "outbox",
                "continuations",
                "generated_state_receipts",
                "conversation_turns",
                "command_results",
            )
        ):
            raise LocalError("deployment_mismatch")

    def initialize_enrollment(self, identity_json, credential_binding_sha256):
        self._owned()
        value = _deployment.identity(identity_json)
        if value["kind"] != "credential_bound":
            raise LocalError("deployment_mismatch")
        _deployment.digest(credential_binding_sha256)
        with self._db() as db, transaction(db):
            if db.execute("SELECT 1 FROM deployments").fetchone():
                existing = self._read_deployment(db)
                if (
                    existing.identity_json != identity_json
                    or existing.credential_binding_sha256 != credential_binding_sha256
                ):
                    raise LocalError("deployment_mismatch")
                return existing
            self._empty(db)
            identifier = str(uuid4())
            db.execute(
                "INSERT INTO deployments(id,format_version,created_at,identity_kind,endpoint,agent_name,credential_binding_sha256) VALUES (?,1,?,?,?,?,?)",
                (
                    identifier,
                    now(),
                    "credential_bound",
                    value["endpoint"],
                    value["agent_name"],
                    credential_binding_sha256,
                ),
            )
            self._deployment_id = identifier
            self.fault("after_enrollment_insert")
            return self._read_deployment(db)

    def bind_deployment(self, deployment_id, manifest_json):
        self._owned()
        candidate = _deployment.manifest(manifest_json)
        if candidate["deployment_id"] != deployment_id:
            raise LocalError("deployment_mismatch")
        with self._db() as db, transaction(db):
            current = self._read_deployment(db)
            if current.deployment_id != deployment_id or candidate[
                "identity"
            ] != _deployment.identity(current.identity_json):
                raise LocalError("deployment_mismatch")
            if current.manifest_json is not None:
                if current.manifest_json != manifest_json:
                    raise LocalError("deployment_mismatch")
                return
            self._empty(db)
            db.execute(
                "UPDATE deployments SET manifest_json=? WHERE id=? AND manifest_json IS NULL",
                (manifest_json, deployment_id),
            )
            self.fault("after_manifest_bind")

    def prepare(self, attempt):
        raise ConfigurationError("unsupported_execution_method")

    def _continuation_value(self, db, thread_id, attempt_key):
        self._thread(db, thread_id)
        row = db.execute(
            "SELECT * FROM continuations WHERE deployment_id=? AND thread_id=? AND attempt_key=?",
            (self._deployment_id, thread_id, attempt_key),
        ).fetchone()
        if row is None:
            return None
        value = DurableContinuation(**dict(row))
        self._validate_continuation(value)
        return value

    def _validate_continuation(self, value):
        if value.deployment_id != self._deployment_id or value.kind not in {
            "business_write",
            "state_write",
            "delegation",
        }:
            raise LocalError("attempt_conflict")
        _uuid(value.thread_id)
        _uuid(value.decision_id)
        path = _deployment.parse(value.child_path_json)
        if (
            type(path) is not list
            or len(path) > 16
            or any(type(p) is not str or not p or len(p.encode()) > 256 for p in path)
            or type(value.checkpoint_namespace) is not str
            or len(value.checkpoint_namespace.encode()) > 8192
            or type(value.native_tool_call_id) is not str
            or not value.native_tool_call_id
            or len(value.native_tool_call_id.encode()) > 256
        ):
            raise LocalError("attempt_conflict")
        context = DurableCallContext(
            tuple(path),
            value.checkpoint_namespace,
            value.parent_attempt_key,
            value.kind,
        )
        if durable_attempt_key(context, value.native_tool_call_id) != value.attempt_key:
            raise LocalError("attempt_conflict")
        if bool(path) != (value.parent_attempt_key is not None):
            raise LocalError("attempt_conflict")

    def continuation(self, thread_id, attempt_key):
        with self._db() as db:
            return self._continuation_value(db, thread_id, attempt_key)

    def prepare_continuation(self, attempt, continuation):
        self._locked(attempt.thread_id)
        self._validate_continuation(continuation)
        if (
            attempt.deployment_id != self._deployment_id
            or attempt.thread_id != continuation.thread_id
            or attempt.decision_id != continuation.decision_id
            or attempt.tool_call_id != continuation.attempt_key
            or attempt.status != "prepared"
            or attempt.revision
            or attempt.result_json
            or attempt.pause_event_json
            or attempt.resume_event_json
            or attempt.terminal_event_json
        ):
            raise LocalError("invalid_attempt")
        native = _json(attempt.native_call_json)
        if native.get("id") != continuation.native_tool_call_id:
            raise LocalError("invalid_attempt")
        if attempt.idempotency_key is not None:
            _uuid(attempt.idempotency_key)
            if native["args"].get("idempotency_key") != attempt.idempotency_key:
                raise LocalError("invalid_attempt")
        event = _json(attempt.event_template_json)
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
            previous = self.get_attempt(attempt.thread_id, attempt.tool_call_id)
            if previous is not None:
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
                if (
                    any(
                        getattr(previous, k) != v
                        for k, v in asdict(attempt).items()
                        if k not in mutable
                    )
                    or self._continuation_value(
                        db, attempt.thread_id, attempt.tool_call_id
                    )
                    != continuation
                ):
                    raise LocalError("attempt_conflict")
                return previous
            if continuation.parent_attempt_key is not None:
                parent = self._continuation_value(
                    db, attempt.thread_id, continuation.parent_attempt_key
                )
                parent_attempt = self.get_attempt(
                    attempt.thread_id, continuation.parent_attempt_key
                )
                if (
                    parent is None
                    or parent.kind != "delegation"
                    or parent_attempt.status != "executing"
                ):
                    raise LocalError("attempt_conflict")
            _insert(db, "attempts", attempt)
            _insert(db, "continuations", continuation)
            self.fault("after_prepare_continuation")
            return attempt

    def generated_state_receipt(self, thread_id, attempt_key):
        with self._db() as db:
            continuation = self._continuation_value(db, thread_id, attempt_key)
            if continuation is None:
                return None
            row = db.execute(
                "SELECT * FROM generated_state_receipts WHERE deployment_id=? AND thread_id=? AND decision_id=?",
                (self._deployment_id, thread_id, continuation.decision_id),
            ).fetchone()
            if row is None:
                return None
            result = GeneratedStateReceipt(**dict(row))
            self._validate_receipt(result, continuation)
            return result

    def _validate_receipt(self, receipt, continuation):
        if (
            (receipt.deployment_id, receipt.thread_id, receipt.decision_id)
            != (self._deployment_id, continuation.thread_id, continuation.decision_id)
            or type(receipt.format_version) is not int
            or receipt.format_version != 1
            or continuation.kind not in {"state_write", "delegation"}
        ):
            raise LocalError("attempt_conflict")
        _uuid(receipt.input_checkpoint_id)
        decode_state_update(receipt.update_json)

    def settle_attempt(self, thread_id, decision_id, expected_revision, settlement):
        self._locked(thread_id)
        with self._db() as db:
            attempt = self._attempt(db, thread_id, decision_id)
            continuation = self._continuation_value(db, thread_id, attempt.tool_call_id)
            if continuation is None:
                raise LocalError("attempt_conflict")
            if settlement.status == "succeeded" and continuation.kind in {
                "state_write",
                "delegation",
            }:
                if (
                    self.generated_state_receipt(thread_id, attempt.tool_call_id)
                    is None
                ):
                    raise LocalError("invalid_settlement")
            return super().settle_attempt(
                thread_id, decision_id, expected_revision, settlement
            )

    def settle_generated_attempt(
        self, thread_id, attempt_key, expected_revision, settlement, receipt
    ):
        self._locked(thread_id)
        with self._db() as db, transaction(db):
            continuation = self._continuation_value(db, thread_id, attempt_key)
            if continuation is None:
                raise LocalError("attempt_conflict")
            self._validate_receipt(receipt, continuation)
            existing = self.generated_state_receipt(thread_id, attempt_key)
            if existing is not None:
                if existing != receipt:
                    raise LocalError("settlement_conflict")
            else:
                _insert(db, "generated_state_receipts", receipt)
            self.fault("after_generated_receipt_insert")
            return self.settle_attempt(
                thread_id, continuation.decision_id, expected_revision, settlement
            )

    def admit(self, *, kind, command_id, thread_id=None, message=None):
        from openeight.langchain.durable import (
            DurableCommand,
            DurableObserveRecord,
            DurableThread,
        )
        from openeight.telemetry import uuid7

        self._owned()
        request = command_request(
            kind=kind, command_id=command_id, thread_id=thread_id, message=message
        )
        with self._db() as db, transaction(db):
            deployment = self._read_deployment(db)
            if deployment.manifest_json is None:
                raise ConfigurationError("durable_deployment_unbound")
            old = db.execute(
                "SELECT * FROM commands WHERE deployment_id=? AND id=?",
                (self._deployment_id, command_id),
            ).fetchone()
            if old:
                command = DurableCommand(**dict(old))
                if command.request_json != request:
                    raise LocalError("command_conflict")
                return DurableAdmission(
                    self._deployment_id,
                    command.thread_id,
                    command.id,
                    self._thread(db, command.thread_id).run_id,
                )
            ts = now()
            if kind == "start":
                thread_id = str(uuid4())
                run_id = uuid7()
                thread = DurableThread(
                    self._deployment_id, thread_id, thread_id, run_id, ts, ts, ts
                )
                active = DurableObserveRecord(
                    run_id,
                    thread_id,
                    "run_active",
                    canonical(
                        {
                            "id": run_id,
                            "started_at": ts,
                            "status": "active",
                            "attributes_json": "{}",
                        }
                    ),
                    ts,
                )
                self._create(db, thread, active)
            else:
                thread = self._thread(db, thread_id)
                if (
                    kind == "reply"
                    and thread.status != "waiting_customer"
                    or kind == "resume"
                    and thread.status
                    not in {"active", "waiting", "reconciliation_required", "completed"}
                ):
                    raise LocalError("command_conflict")
                if kind == "reply" and thread.run_ended_at:
                    raise LocalError("command_conflict")
                run_id = thread.run_id
            recover = None
            if thread.active_command_id:
                active = self._command(db, thread_id, thread.active_command_id)
                if kind != "resume" or active.status not in {
                    "running",
                    "reconciliation_required",
                }:
                    raise LocalError("execution_busy")
                recover = active.id
            command = DurableCommand(
                self._deployment_id,
                command_id,
                thread_id,
                kind,
                request,
                ts,
                ts,
                recover_command_id=recover,
            )
            _insert(db, "commands", command)
            if kind != "resume":
                namespace = uuid5(
                    NAMESPACE_URL,
                    canonical(
                        ["openeight-local-command-v1", self._deployment_id, command_id]
                    ),
                )
                db.execute(
                    "INSERT INTO conversation_turns VALUES (?,?,?,?,?,?,?,?)",
                    (
                        self._deployment_id,
                        str(uuid5(namespace, "customer")),
                        thread_id,
                        command_id,
                        0,
                        "customer",
                        message,
                        ts,
                    ),
                )
            self.fault("after_command_admission")
            return DurableAdmission(self._deployment_id, thread_id, command_id, run_id)

    def _result_value(self, db, thread_id, raw):
        from openeight.langchain.durable import DurableAgentResult

        value = _deployment.parse(raw)
        thread = self._thread(db, thread_id)
        if (
            type(value) is not dict
            or set(value)
            != {"thread_id", "run_id", "status", "pending_decision_ids", "output"}
            or value["thread_id"] != thread.id
            or value["run_id"] != thread.run_id
            or type(value["status"]) is not str
            or value["status"]
            not in {
                "waiting",
                "waiting_customer",
                "completed",
                "reconciliation_required",
            }
        ):
            raise LocalError("command_conflict")
        pending = value["pending_decision_ids"]
        output = value["output"]
        if (
            type(pending) is not list
            or len(pending) > 64
            or any(type(v) is not str for v in pending)
            or len(set(pending)) != len(pending)
            or type(output) is not list
            or len(output) > 16
            or any(type(v) is not str or len(v.encode()) > 8192 for v in output)
        ):
            raise LocalError("command_conflict")
        known = {a.decision_id for a in self.attempts(thread_id)}
        for identifier in pending:
            _uuid(identifier)
            if identifier not in known:
                raise LocalError("command_conflict")
        return DurableAgentResult(
            thread.id, thread.run_id, value["status"], tuple(pending), tuple(output)
        )

    def _snapshot_command_result(self, db, command, *, recovered_output=None):
        previous = db.execute(
            "SELECT result_json FROM command_results WHERE deployment_id=? AND command_id=? AND command_revision=?",
            (self._deployment_id, command.id, command.revision),
        ).fetchone()
        if previous:
            self._result_value(db, command.thread_id, previous["result_json"])
            return
        thread = self._thread(db, command.thread_id)
        value = {
            "thread_id": thread.id,
            "run_id": thread.run_id,
            "status": thread.status,
            "pending_decision_ids": [
                a.decision_id for a in self.attempts(thread.id) if a.status == "waiting"
            ],
            "output": [
                r["body"]
                for r in db.execute(
                    "SELECT body FROM conversation_turns WHERE deployment_id=? AND command_id=? AND speaker='agent' ORDER BY ordinal",
                    (self._deployment_id, command.id),
                )
            ],
        }
        if recovered_output is not None:
            value["output"] = list(recovered_output)
        raw = _deployment.canonical(value)
        self._result_value(db, thread.id, raw)
        db.execute(
            "INSERT INTO command_results VALUES (?,?,?,?,?)",
            (self._deployment_id, thread.id, command.id, command.revision, raw),
        )
        self.fault("after_command_result_snapshot")

    def settle_command(self, thread_id, command_id, expected_revision, settlement):
        self._locked(thread_id)
        with self._db() as db, transaction(db):
            command = self._command(db, thread_id, command_id)
            recovered = self._recovery_chain(db, command)
            previous_revisions = {c.id: c.revision for c in [command, *recovered]}
            saved = super().settle_command(
                thread_id, command_id, expected_revision, settlement
            )
            for identifier in [saved.id, *[c.id for c in recovered]]:
                current = self._command(db, thread_id, identifier)
                if (
                    current.revision == previous_revisions[identifier]
                    and not db.execute(
                        "SELECT 1 FROM command_results WHERE deployment_id=? AND command_id=? AND command_revision=?",
                        (self._deployment_id, current.id, current.revision),
                    ).fetchone()
                ):
                    raise LocalError("command_conflict")
                self._snapshot_command_result(
                    db,
                    current,
                    recovered_output=settlement.recovered_turns
                    if settlement.recovered_command_id is not None
                    else None,
                )
            return saved

    def command_result(self, thread_id, command_id):
        from openeight.langchain.durable import DurableAgentResult

        with self._db() as db:
            thread = self._thread(db, thread_id)
            command = self._command(db, thread_id, command_id)
            row = db.execute(
                "SELECT result_json FROM command_results WHERE deployment_id=? AND thread_id=? AND command_id=? AND command_revision=?",
                (self._deployment_id, thread_id, command_id, command.revision),
            ).fetchone()
            if command.status in {"accepted", "running"}:
                if row is not None:
                    raise LocalError("command_conflict")
                if command.status == "accepted":
                    return None
                return DurableAgentResult(
                    thread.id, thread.run_id, "recovery_required", (), ()
                )
            if row is not None:
                return self._result_value(db, thread_id, row["result_json"])
            if command.status == "waiting" and command.reason_code == "interrupted":
                return DurableAgentResult(
                    thread.id, thread.run_id, "recovery_required", (), ()
                )
            raise LocalError("command_conflict")

    def thread_status(self, thread_id):
        from ..durable import DurableThreadStatus

        self._locked(thread_id)
        with self._db() as db:
            thread = self._thread(db, thread_id)
            pending = tuple(
                row[0]
                for row in db.execute(
                    "SELECT decision_id FROM attempts WHERE deployment_id=? AND thread_id=? AND status='waiting' ORDER BY created_at,decision_id",
                    (self._deployment_id, thread_id),
                )
            )
            return DurableThreadStatus(thread.id, thread.run_id, thread.status, pending)


@dataclass(frozen=True)
class DurableAdmission:
    deployment_id: str
    thread_id: str
    command_id: str
    run_id: str
