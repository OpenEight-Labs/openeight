"""SDK-owned journal transitions retaining the qualified durable record contract."""

import json
from dataclasses import asdict, replace
from uuid import uuid4

from openeight._durable_sqlite import JournalTransitions as SharedJournalTransitions
from openeight._durable_sqlite import _insert, _json, _uuid
from openeight.langchain.durable import (
    DurableAttempt,
    DurableCommand,
    DurableObserveRecord,
    DurableThread,
)

from ._filesystem import LocalError, now, transaction


class JournalTransitions(SharedJournalTransitions):
    def _thread(self, db, thread_id):
        row = db.execute(
            "SELECT * FROM threads WHERE deployment_id=? AND id=?",
            (self._deployment_id, thread_id),
        ).fetchone()
        if row is None:
            raise LocalError("not_found")
        return self._thread_value(row)

    def load(self, thread_id):
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM threads WHERE deployment_id=? AND id=?",
                (self._deployment_id, thread_id),
            ).fetchone()
            return self._thread_value(row) if row else None

    def _command(self, db, thread_id, command_id):
        row = db.execute(
            "SELECT * FROM commands WHERE deployment_id=? AND thread_id=? AND id=?",
            (self._deployment_id, thread_id, command_id),
        ).fetchone()
        if row is None:
            raise LocalError("not_found")
        return DurableCommand(**dict(row))

    def _record(self, db, record):
        self._mode("integrated")
        thread = self._thread(db, record.thread_id)
        payload = _json(record.payload_json, 1044480)
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
            (self._deployment_id, record.id),
        ).fetchone()
        data = {"deployment_id": self._deployment_id, **asdict(record)}
        if previous:
            if any(previous[k] != v for k, v in data.items() if k != "status"):
                raise LocalError("outbox_conflict")
            return
        db.execute(
            "INSERT INTO outbox (deployment_id,id,thread_id,kind,payload_json,status,created_at) VALUES (?,?,?,?,?,?,?)",
            (
                self._deployment_id,
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
            thread.deployment_id != self._deployment_id
            or thread.revision
            or thread.run_ended_at
            or thread.status != "active"
            or active.kind != "run_active"
            or active.thread_id != thread.id
        ):
            raise LocalError("invalid_thread")
        _uuid(thread.id)
        _uuid(thread.run_id)
        _insert(db, "threads", thread)
        self._record(db, active)

    def _update(self, db, table, key, record, changes):
        revision = record.revision
        changes = {**changes, "revision": revision + 1, "updated_at": now()}
        assignments = ",".join(k + "=?" for k in changes)
        result = db.execute(
            f"UPDATE {table} SET {assignments} WHERE deployment_id=? AND {key}=? AND revision=?",
            (*changes.values(), self._deployment_id, getattr(record, key), revision),
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
                    "commands",
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
            return self._update(db, "commands", "id", command, {"status": "running"})

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
                "commands",
                "id",
                command,
                {"input_recorded": 1, "input_checkpoint_id": checkpoint_id},
            )

    def _attempt(self, db, thread_id, decision_id):
        row = db.execute(
            "SELECT * FROM attempts WHERE deployment_id=? AND thread_id=? AND decision_id=?",
            (self._deployment_id, thread_id, decision_id),
        ).fetchone()
        if row is None:
            raise LocalError("not_found")
        return DurableAttempt(**dict(row))

    def get_attempt(self, thread_id, tool_call_id):
        with self._db() as db:
            self._thread(db, thread_id)
            row = db.execute(
                "SELECT * FROM attempts WHERE deployment_id=? AND thread_id=? AND tool_call_id=?",
                (self._deployment_id, thread_id, tool_call_id),
            ).fetchone()
            return DurableAttempt(**dict(row)) if row else None

    def attempts(self, thread_id):
        with self._db() as db:
            self._thread(db, thread_id)
            return tuple(
                DurableAttempt(**dict(row))
                for row in db.execute(
                    "SELECT * FROM attempts WHERE deployment_id=? AND thread_id=? ORDER BY created_at,decision_id",
                    (self._deployment_id, thread_id),
                )
            )

    def pending(self, thread_id):
        with self._db() as db:
            self._thread(db, thread_id)
            rows = db.execute(
                "SELECT id,thread_id,kind,payload_json,created_at,status FROM outbox WHERE deployment_id=? AND thread_id=? AND status=?",
                (self._deployment_id, thread_id, "pending"),
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
                (self._deployment_id, thread_id, record_id),
            ).fetchone()
            if not row:
                raise LocalError("not_found")
            if row["status"] == "acknowledged":
                return
            db.execute(
                "UPDATE outbox SET status='acknowledged' WHERE deployment_id=? AND id=?",
                (self._deployment_id, record_id),
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
                (self._deployment_id, thread_id),
            ).fetchone():
                raise LocalError("evidence_pending")
            if db.execute(
                "SELECT 1 FROM attempts WHERE deployment_id=? AND thread_id=? AND status NOT IN ('succeeded','failed','blocked')",
                (self._deployment_id, thread_id),
            ).fetchone():
                raise LocalError("attempt_pending")
            payload = _json(record.payload_json)
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
                (self._deployment_id, command.id, ordinal),
            ).fetchone()
            if previous:
                if previous["body"] != body or previous["speaker"] != "agent":
                    raise LocalError("command_conflict")
                continue
            db.execute(
                "INSERT INTO conversation_turns VALUES (?,?,?,?,?,?,?,?)",
                (
                    self._deployment_id,
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
                        (self._deployment_id, command.id),
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
                            (self._deployment_id, root.id),
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
            table = "attempts"
            unfinished = db.execute(
                f"SELECT status FROM {table} WHERE deployment_id=? AND thread_id=? AND status NOT IN ('succeeded','failed','blocked')",
                (self._deployment_id, thread_id),
            ).fetchall()
            if unfinished and settlement.thread_status not in {
                "waiting",
                "reconciliation_required",
            }:
                raise LocalError("attempt_pending")
            if settlement.thread_status == "completed" and thread.status != "completed":
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
                        "commands",
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
                "commands",
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

    def _mode(self, mode):
        if mode != "integrated":
            raise LocalError("execution_mode_mismatch")

    def _thread_value(self, row):
        if not row["run_id"] or not row["run_started_at"]:
            raise LocalError("execution_mode_mismatch")
        return DurableThread(**dict(row))

    def prior_customer_inputs(self, thread_id, command_id):
        with self._db() as db:
            self._thread(db, thread_id)
            self._command(db, thread_id, command_id)
        return ()
