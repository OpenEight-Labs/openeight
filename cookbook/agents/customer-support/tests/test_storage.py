"""Atomic durable admission/settlement and stable local ownership."""

from dataclasses import replace
from uuid import uuid4

import pytest

from openeight import Event
from openeight.langchain.durable import (
    AttemptSettlement,
    AttemptTransition,
    CommandSettlement,
    DurableAttempt,
    DurableObserveRecord,
)
from openeight.telemetry import uuid7
from local import Deployment, LocalError, canonical, now
from storage import SQLiteExecutionStore


@pytest.fixture
def store(tmp_path):
    d = Deployment(
        "deployment",
        "http://127.0.0.1:8080",
        "workspace",
        "agent",
        "agent-id",
        "key-id",
        "integrated",
        4,
        now(),
    )
    s = SQLiteExecutionStore(tmp_path / "state", d)
    s.initialize()
    return s


def start(s):
    request = {
        "kind": "start",
        "command_id": str(uuid4()),
        "case_id": "case_damaged",
        "message": "Damaged",
    }
    command, new = s.admit(request, case_id="case_damaged")
    assert new
    return command, request


def executing(s, command):
    thread = s.load(command.thread_id)
    command = s.begin_command(thread.id, command.id, command.revision, thread.revision)
    ts, decision, event_id = now(), uuid7(), uuid7()
    native = {
        "id": "call",
        "type": "tool_call",
        "name": "issue_refund",
        "args": {
            "action_id": "action",
            "facts_revision": 2,
            "amount": 180,
            "currency": "USD",
            "idempotency_key": str(uuid4()),
        },
    }
    selected = {k: v for k, v in native["args"].items() if k != "idempotency_key"}
    event = Event(
        "langchain.tool",
        "tool_call",
        "invoke",
        ts,
        "success",
        decision_id=decision,
        input=selected,
        tool_id="01900000-0000-7000-8000-000000000001",
        tool_version_id="01900000-0000-7000-8000-000000000002",
        tool_name="issue_refund",
    )._as_write()
    event.update(id=event_id, run_id=thread.run_id)
    attempt = DurableAttempt(
        s.deployment.id,
        decision,
        thread.id,
        "call",
        "issue_refund",
        event["tool_id"],
        event["tool_version_id"],
        "2" * 64,
        ts,
        event_id,
        canonical(native),
        canonical(
            {
                "input": {},
                "policy_contract_set_digest": "2" * 64,
                "selected_values": [
                    {"path": key, "value": value}
                    for key, value in sorted(selected.items())
                ],
            }
        ),
        canonical(event),
        ts,
        ts,
        idempotency_key=native["args"]["idempotency_key"],
    )
    attempt = s.prepare(attempt)
    attempt = s.transition_attempt(
        thread.id, decision, attempt.revision, AttemptTransition("executing")
    )
    return command, attempt


def settlement(a):
    event = DurableObserveRecord(
        a.event_id, a.thread_id, "event", a.event_template_json, a.source_time
    )
    return AttemptSettlement(
        "succeeded", canonical({"content": "receipt", "status": "success"}), event=event
    )


def test_admission_retry_and_changed_payload(store):
    c, request = start(store)
    repeated, new = store.admit(request, case_id="case_damaged")
    assert repeated == c and not new
    assert len(store.transcript(c.thread_id)) == 1
    with pytest.raises(LocalError, match="command_conflict"):
        store.admit({**request, "message": "edited"}, case_id="case_damaged")
    with pytest.raises(LocalError, match="case_busy"):
        store.admit({**request, "command_id": str(uuid4())}, case_id="case_damaged")


@pytest.mark.parametrize(
    "fault_point", ["after_attempt_result", "after_attempt_outbox"]
)
def test_settlement_result_and_outbox_rollback_together(store, fault_point):
    command, _ = start(store)
    with store.acquire(command.thread_id):
        command, a = executing(store, command)

        def crash(point):
            if point == fault_point:
                raise RuntimeError("synthetic crash")

        store.fault = crash
        with pytest.raises(RuntimeError):
            store.settle_attempt(a.thread_id, a.decision_id, a.revision, settlement(a))
        assert store.get_attempt(a.thread_id, a.tool_call_id).status == "executing"
        assert [r.kind for r in store.pending(a.thread_id)] == ["run_active"]
        store.fault = lambda _: None
        result = store.settle_attempt(
            a.thread_id, a.decision_id, a.revision, settlement(a)
        )
        assert result.status == "succeeded"
        assert [r.kind for r in store.pending(a.thread_id)] == ["run_active", "event"]
        assert (
            store.settle_attempt(a.thread_id, a.decision_id, a.revision, settlement(a))
            == result
        )
        with pytest.raises(LocalError, match="settlement_conflict"):
            store.settle_attempt(
                a.thread_id,
                a.decision_id,
                a.revision,
                replace(settlement(a), result_json="{}"),
            )


def test_template_change_refused(store):
    command, _ = start(store)
    with store.acquire(command.thread_id):
        _, a = executing(store, command)
        result = settlement(a)
        bad = replace(
            result.event,
            payload_json=result.event.payload_json.replace("langchain.tool", "changed"),
        )
        with pytest.raises(LocalError, match="invalid_settlement"):
            store.settle_attempt(
                a.thread_id, a.decision_id, a.revision, replace(result, event=bad)
            )
        assert store.get_attempt(a.thread_id, "call").status == "executing"


def test_command_input_and_turn_settlement(store):
    c, _ = start(store)
    with store.acquire(c.thread_id):
        thread = store.load(c.thread_id)
        c = store.begin_command(c.thread_id, c.id, c.revision, thread.revision)
        c = store.record_command_input(c.thread_id, c.id, c.revision, None)
        assert c.input_recorded == 1 and c.input_checkpoint_id is None
        assert store.record_command_input(c.thread_id, c.id, c.revision, None) == c
        with pytest.raises(LocalError, match="command_conflict"):
            store.record_command_input(c.thread_id, c.id, c.revision, "different")
        thread = store.load(c.thread_id)
        completion = CommandSettlement(
            "completed",
            thread.revision,
            "waiting_customer",
            "checkpoint",
            ("Which remedy?",),
        )
        result = store.settle_command(c.thread_id, c.id, c.revision, completion)
        assert result.status == "completed"
        assert store.load(c.thread_id).active_command_id is None
        assert store.load(c.thread_id).status == "waiting_customer"
        assert store.settle_command(c.thread_id, c.id, c.revision, completion) == result
    assert [r["body"] for r in store.transcript(c.thread_id)] == [
        "Damaged",
        "Which remedy?",
    ]


def test_interrupted_command_requires_explicit_recovery(store):
    c, request = start(store)
    with store.acquire(c.thread_id):
        thread = store.load(c.thread_id)
        c = store.begin_command(c.thread_id, c.id, c.revision, thread.revision)
    assert not store.admit(request, case_id="case_damaged")[1]
    with pytest.raises(LocalError, match="thread_busy"):
        store.admit(
            {
                "kind": "reply",
                "command_id": str(uuid4()),
                "thread_id": c.thread_id,
                "message": "new",
            }
        )
    resume, new = store.admit(
        {"kind": "resume", "command_id": str(uuid4()), "thread_id": c.thread_id}
    )
    assert new and resume.recover_command_id == c.id
    with store.acquire(c.thread_id):
        thread = store.load(c.thread_id)
        resume = store.begin_command(
            c.thread_id, resume.id, resume.revision, thread.revision
        )
        assert store.command(c.thread_id, c.id).reason_code == "interrupted"
        assert store.load(c.thread_id).observation_incomplete == 1


def test_nonblocking_lock_order_and_stable_inodes(store):
    c, _ = start(store)
    lock = store.locks.directory / ("thread-" + c.thread_id)
    with store.acquire(c.thread_id):
        inode = lock.stat().st_ino
        with pytest.raises(LocalError, match="execution_busy"):
            with store.acquire(c.thread_id):
                pass
        with pytest.raises(LocalError, match="execution_busy"):
            with store.locks.hold("lifecycle"):
                pass
    with store.acquire(c.thread_id):
        assert lock.stat().st_ino == inode


def test_sqlite_brief_contention_settles_but_long_contention_has_no_effect(tmp_path):
    import threading
    from local import connection, transaction

    path = tmp_path / "contention.sqlite"
    with connection(path) as db:
        db.execute("CREATE TABLE effects (id INTEGER PRIMARY KEY)")
    held, release = threading.Event(), threading.Event()

    def holder():
        with connection(path) as db, transaction(db):
            held.set()
            assert release.wait(2)

    worker = threading.Thread(target=holder)
    worker.start()
    assert held.wait(2)
    timer = threading.Timer(0.05, release.set)
    timer.start()
    try:
        with connection(path) as db, transaction(db):
            db.execute("INSERT INTO effects VALUES (1)")
    finally:
        release.set()
        timer.join()
        worker.join()
    with connection(path) as held_db, transaction(held_db):
        with pytest.raises(LocalError, match="storage_unavailable"):
            with connection(path) as blocked_db, transaction(blocked_db):
                blocked_db.execute("INSERT INTO effects VALUES (2)")
    with connection(path) as db:
        assert [r["id"] for r in db.execute("SELECT id FROM effects")] == [1]


def test_scope_and_ownership_fail_closed(store):
    c, _ = start(store)
    with pytest.raises(LocalError, match="execution_not_owned"):
        store.begin_command(c.thread_id, c.id, 0, 0)
    with pytest.raises(LocalError, match="not_found"):
        store.command(str(uuid4()), c.id)
    foreign = SQLiteExecutionStore(
        store.state, replace(store.deployment, workspace_id="other")
    )
    with pytest.raises(LocalError, match="deployment_mismatch"):
        foreign.load(c.thread_id)


def test_refused_recovery_is_not_automatically_redispatched(store):
    c, _ = start(store)
    with store.acquire(c.thread_id):
        thread = store.load(c.thread_id)
        store.begin_command(thread.id, c.id, c.revision, thread.revision)
    resume, _ = store.admit(
        {"kind": "resume", "command_id": str(uuid4()), "thread_id": c.thread_id}
    )
    store.refuse_dispatch(resume.id)
    assert store.find_command(resume.id).status == "reconciliation_required"
    assert store.commands_for_dispatch() == ()
    assert store.find_command(c.id).status == "waiting"
    next_resume, _ = store.admit(
        {"kind": "resume", "command_id": str(uuid4()), "thread_id": c.thread_id}
    )
    assert next_resume.recover_command_id == resume.id


@pytest.mark.parametrize("initial,current", [(None, "a" * 64), ("a" * 64, "b" * 64)])
def test_catalog_accepts_governance_changes_without_schema_successor(store, initial, current):
    from types import SimpleNamespace
    from openeight.tools import ToolRegistration

    fact = ToolRegistration(
        name="issue_refund",
        description="Refund the order",
        input_schema={"type": "object", "properties": {}},
    )
    tool_id, version_id = str(uuid4()), str(uuid4())

    def result(digest, owner=tool_id):
        return SimpleNamespace(
            request_index=0,
            tool=SimpleNamespace(id=owner),
            tool_version=SimpleNamespace(id=version_id, tool_contract_digest=digest),
        )

    store.record_catalog([fact], [result(initial)])
    store.record_catalog([fact], [result(current)])
    store.record_catalog([fact], [result(current)])
    with store._db() as db:
        rows = db.execute(
            "SELECT tool_id,tool_version_id,tool_contract_digest FROM catalog_versions"
        ).fetchall()
    assert [tuple(row) for row in rows] == [(tool_id, version_id, current)]
    with pytest.raises(LocalError, match="tool_catalog_conflict"):
        store.record_catalog([fact], [result(current, str(uuid4()))])
    changed = ToolRegistration(
        name="issue_refund",
        description="Refund the order",
        input_schema={"type": "object", "properties": {"amount": {"type": "number"}}},
    )
    with pytest.raises(LocalError, match="tool_schema_changed"):
        store.record_catalog([changed], [result(current)])


@pytest.mark.parametrize("output", ['"[REDACTED]"', '{"safe":true}', '17', 'false', 'null', None])
def test_redacted_output_is_independent_of_native_recovery_result(store, output):
    import json
    command, _ = start(store)
    with store.acquire(command.thread_id):
        command, attempt = executing(store, command)
        original = settlement(attempt)
        payload = json.loads(original.event.payload_json)
        payload.pop("output_json", None)
        if output is not None:
            payload["output_json"] = output
        event = replace(original.event, payload_json=canonical(payload))
        update = replace(original, result_json=canonical({"content": "NATIVE-SECRET", "status": "success"}), event=event)
        saved = store.settle_attempt(attempt.thread_id, attempt.decision_id, attempt.revision, update)
        assert "NATIVE-SECRET" in saved.result_json
        assert "NATIVE-SECRET" not in saved.terminal_event_json
        assert saved.terminal_event_json == event.payload_json
        assert store.settle_attempt(attempt.thread_id, attempt.decision_id, attempt.revision, update) == saved
        frozen = next(row for row in store.pending(attempt.thread_id) if row.id == attempt.event_id)
        assert frozen.payload_json == event.payload_json


@pytest.mark.parametrize("field,value", [
    ("output_json", ' {"safe":true}'),
    ("output_json", '"' + 'x' * 16383 + '"'),
    ("output_json", 'NaN'),
    ("output_json", '{"a":1,"a":1}'),
    ("output_json", '1e999'),
    ("output_json", '9223372036854775808'),
    ("output_json", None),
    ("decision_id", "tampered"), ("tool_id", "tampered"), ("run_id", "tampered"),
    ("input_json", '{}'), ("outcome", "error"), ("occurred_at", "invalid"),
    ("latency_ms", -1), ("attributes_json", '{"timing_source":"invented"}'),
])
def test_redaction_does_not_relax_other_settlement_integrity(store, field, value):
    import json
    command, _ = start(store)
    with store.acquire(command.thread_id):
        command, attempt = executing(store, command)
        update = settlement(attempt)
        payload = json.loads(update.event.payload_json)
        payload[field] = value
        update = replace(update, event=replace(update.event, payload_json=canonical(payload)))
        with pytest.raises(LocalError, match="invalid_settlement"):
            store.settle_attempt(attempt.thread_id, attempt.decision_id, attempt.revision, update)
        assert store.get_attempt(attempt.thread_id, attempt.tool_call_id).status == "executing"
        assert not any(row.id == attempt.event_id for row in store.pending(attempt.thread_id))


def test_failed_settlement_forbids_even_valid_redacted_output(store):
    import json
    command, _ = start(store)
    with store.acquire(command.thread_id):
        command, attempt = executing(store, command)
        update = settlement(attempt)
        payload = json.loads(update.event.payload_json)
        payload.update(outcome="error", error_json=canonical({"kind": "tool_error"}), output_json='"[REDACTED]"')
        update = replace(update, status="failed", result_json=canonical({"content": "failed", "status": "error"}),
                         event=replace(update.event, payload_json=canonical(payload)))
        with pytest.raises(LocalError, match="invalid_settlement"):
            store.settle_attempt(attempt.thread_id, attempt.decision_id, attempt.revision, update)
