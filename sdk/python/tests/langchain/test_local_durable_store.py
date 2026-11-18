"""SQLite lifecycle evidence, separate from real-app HTTP governance integration."""

import hashlib
import os
import sqlite3
from dataclasses import replace
from uuid import uuid4

import pytest

pytest.importorskip("deepagents")

from openeight import ConfigurationError, Event
from openeight.deepagents._continuation import DurableCallContext, durable_attempt_key
from openeight.deepagents._deployment import canonical
from openeight.deepagents._frameworks._state import encode_state_update
from openeight.deepagents._local_durable._filesystem import LocalError, now
from openeight.deepagents._local_durable._store import SQLiteDeepAgentsStore
from openeight.deepagents.durable import DurableContinuation, GeneratedStateReceipt
from openeight.langchain.durable import (
    AttemptSettlement,
    AttemptTransition,
    DurableAttempt,
    DurableObserveRecord,
)
from openeight.telemetry import uuid7

IDENTITY = dict(
    kind="credential_bound",
    endpoint="http://localhost:7777",
    agent_name="test",
    organization_id=None,
    workspace_id=None,
    agent_id=None,
    api_key_id=None,
)


def manifest(deployment):
    return measured_manifest(
        dict(
            format_version=2,
            integration="deepagents",
            deployment_id=deployment.deployment_id,
            identity=IDENTITY,
            application={"kind": "revision", "value": "test-release"},
            configuration_sha256="a" * 64,
            packages={
                k: "1.0.0"
                for k in (
                    "openeight",
                    "deepagents",
                    "langchain",
                    "langchain-core",
                    "langgraph",
                    "langgraph-prebuilt",
                    "langgraph-checkpoint",
                    "langgraph-checkpoint-sqlite",
                    "aiosqlite",
                    "python",
                )
            },
            backend={"kind": "state", "format_version": 1},
            saver={
                "kind": "async-sqlite",
                "format_version": 1,
                "pickle_fallback": False,
            },
            compatibility={
                "implementation_id": "deepagents-v1",
                "implementation_revision": 1,
                "qualification_id": "storage-fixture",
                "checkpoint_format": "deepagents-sqlite-v1",
            },
            catalog_naming="hashed-v1",
            topology=[{"path": [], "delegates_to": [], "tools": []}],
        )
    )


def measured_manifest(document):
    import hashlib

    nodes = []
    for topology in document["topology"]:
        nodes.append(
            {
                "path": topology["path"],
                "delegates_to": topology["delegates_to"],
                "source": "explicit",
                "model_class": "fixture.Model",
                "model_spec_sha256": None,
                "system_message_sha256": "a" * 64,
                "middleware": [],
                "state_schema_kind": "fixture.State",
                "private_state_keys": [],
                "default_config_sha256": "b" * 64,
                "runtime_variant": None,
                "response_format_sha256": None,
                "tools": [
                    {
                        **{
                            key: tool[key]
                            for key in (
                                "native_name",
                                "catalog_name",
                                "input_schema_sha256",
                            )
                        },
                        "executable_description_sha256": "c" * 64,
                        "model_visible_description_sha256": "d" * 64,
                    }
                    for tool in topology["tools"]
                ],
            }
        )
    descriptor = {
        "format_version": 2,
        "nodes": nodes,
        **{key: document[key] for key in ("backend", "saver", "catalog_naming")},
    }
    document["effective_configuration"] = descriptor
    document["effective_configuration_sha256"] = hashlib.sha256(
        canonical(descriptor).encode()
    ).hexdigest()
    return canonical(document)


def bound_store(tmp_path):
    value = SQLiteDeepAgentsStore(tmp_path)
    with value.acquire_deployment():
        deployment = value.initialize_enrollment(canonical(IDENTITY), "a" * 64)
        value.bind_deployment(deployment.deployment_id, manifest(deployment))
    return value


@pytest.fixture
def store(tmp_path):
    return bound_store(tmp_path)


def prepare(store):
    admission = store.admit(kind="start", command_id="stable-start", message="go")
    command = store.command(admission.thread_id, admission.command_id)
    thread = store.load(admission.thread_id)
    store.begin_command(thread.id, command.id, command.revision, thread.revision)
    ts = now()
    decision = uuid7()
    eventid = uuid7()
    context = DurableCallContext((), "tools:" + str(uuid4()), None, "state_write")
    key = durable_attempt_key(context, "native-call")
    event = Event(
        "write file",
        "tool_call",
        "invoke",
        ts,
        "success",
        decision_id=decision,
        tool_id=str(uuid4()),
        tool_version_id=str(uuid4()),
        tool_name="catalog-file",
    )._as_write()
    event.update(id=eventid, run_id=thread.run_id)
    attempt = DurableAttempt(
        admission.deployment_id,
        decision,
        thread.id,
        key,
        "catalog-file",
        event["tool_id"],
        event["tool_version_id"],
        "a" * 64,
        ts,
        eventid,
        canonical({"id": "native-call", "name": "write_file", "args": {}}),
        canonical(
            {"input": {}, "selected_values": [], "policy_contract_set_digest": "a" * 64}
        ),
        canonical(event),
        ts,
        ts,
    )
    continuation = DurableContinuation(
        admission.deployment_id,
        thread.id,
        decision,
        "[]",
        context.checkpoint_namespace,
        "native-call",
        key,
        "state_write",
    )
    attempt = store.prepare_continuation(attempt, continuation)
    attempt = store.transition_attempt(
        thread.id, decision, attempt.revision, AttemptTransition("executing")
    )
    event["latency_ms"] = 0
    record = DurableObserveRecord(eventid, thread.id, "event", canonical(event), ts)
    settlement = AttemptSettlement(
        "succeeded", '{"content":"done","status":"success"}', None, record
    )
    receipt = GeneratedStateReceipt(
        admission.deployment_id,
        thread.id,
        decision,
        1,
        str(uuid4()),
        encode_state_update({}),
    )
    return attempt, continuation, settlement, receipt


def test_protected_journal_enrollment_and_command_ids_are_immutable(store):
    with store.acquire_deployment():
        first = store.admit(kind="start", command_id="stable-start", message="go")
        assert (
            store.admit(kind="start", command_id="stable-start", message="go") == first
        )
        with pytest.raises(ConfigurationError):
            store.admit(kind="start", command_id="stable-start", message="changed")
        with store.acquire(first.thread_id):
            command = store.command(first.thread_id, first.command_id)
            thread = store.load(first.thread_id)
            store.begin_command(
                thread.id, command.id, command.revision, thread.revision
            )
            result = store.command_result(thread.id, command.id)
            assert (
                result.status == "recovery_required" and result.thread_id == thread.id
            )
    assert os.stat(store.state).st_mode & 0o777 == 0o700
    assert os.stat(store.path).st_mode & 0o777 == 0o600


def test_receipt_result_and_outbox_commit_atomically(store):
    with store.acquire_deployment():
        admission = store.admit(kind="start", command_id="stable-start", message="go")
        with store.acquire(admission.thread_id):
            attempt, continuation, settlement, receipt = prepare(store)
            saved = store.settle_generated_attempt(
                attempt.thread_id,
                attempt.tool_call_id,
                attempt.revision,
                settlement,
                receipt,
            )
            assert saved.status == "succeeded"
            assert (
                store.generated_state_receipt(attempt.thread_id, attempt.tool_call_id)
                == receipt
            )
            assert (
                len([r for r in store.pending(attempt.thread_id) if r.kind == "event"])
                == 1
            )
            assert (
                store.settle_generated_attempt(
                    attempt.thread_id,
                    attempt.tool_call_id,
                    attempt.revision,
                    settlement,
                    receipt,
                )
                == saved
            )
            with pytest.raises(ConfigurationError):
                store.settle_generated_attempt(
                    attempt.thread_id,
                    attempt.tool_call_id,
                    attempt.revision,
                    settlement,
                    replace(receipt, input_checkpoint_id=str(uuid4())),
                )


@pytest.mark.parametrize(
    "point",
    ["after_generated_receipt_insert", "after_attempt_result", "after_attempt_outbox"],
)
def test_receipt_failure_rolls_back_all_generated_settlement(store, point):
    with store.acquire_deployment():
        admission = store.admit(kind="start", command_id="stable-start", message="go")
        with store.acquire(admission.thread_id):
            attempt, continuation, settlement, receipt = prepare(store)

            def fault(current):
                if current == point:
                    raise RuntimeError("injected crash boundary")

            store.fault = fault
            with pytest.raises(RuntimeError):
                store.settle_generated_attempt(
                    attempt.thread_id,
                    attempt.tool_call_id,
                    attempt.revision,
                    settlement,
                    receipt,
                )
            assert (
                store.generated_state_receipt(attempt.thread_id, attempt.tool_call_id)
                is None
            )
            assert (
                store.get_attempt(attempt.thread_id, attempt.tool_call_id).status
                == "executing"
            )
            assert not [
                r for r in store.pending(attempt.thread_id) if r.kind == "event"
            ]


def test_generated_success_cannot_bypass_receipt_requirement(store):
    with store.acquire_deployment():
        admission = store.admit(kind="start", command_id="stable-start", message="go")
        with store.acquire(admission.thread_id):
            attempt, continuation, settlement, receipt = prepare(store)
            with pytest.raises(ConfigurationError):
                store.settle_attempt(
                    attempt.thread_id, attempt.decision_id, attempt.revision, settlement
                )
            assert (
                store.get_attempt(attempt.thread_id, attempt.tool_call_id).status
                == "executing"
            )


def test_foreign_schema_rejects_without_modifying_database_or_creating_locks(tmp_path):
    path = tmp_path / "execution.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE foreign_data (value TEXT)")
    path.chmod(0o600)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ConfigurationError):
        SQLiteDeepAgentsStore(tmp_path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert not (tmp_path / ".locks").exists()


def test_two_owners_cannot_hold_deployment_and_thread_lock_requires_owner(store):
    other = SQLiteDeepAgentsStore(store.state)
    with store.acquire_deployment():
        with pytest.raises(ConfigurationError):
            with other.acquire_deployment():
                pytest.fail("concurrent deployment owner")
    with pytest.raises(ConfigurationError):
        with store.acquire(str(uuid4())):
            pytest.fail("thread without deployment owner")


@pytest.mark.parametrize("drift", ["index", "trigger"])
def test_schema_drift_rejects_dropped_indexes_and_foreign_triggers(store, drift):
    with sqlite3.connect(store.path) as db:
        if drift == "index":
            db.execute("DROP INDEX one_deployment")
        else:
            db.execute(
                "CREATE TRIGGER foreign_trigger AFTER INSERT ON commands BEGIN UPDATE sdk_format SET format_version=1; END"
            )
    before = hashlib.sha256(store.path.read_bytes()).hexdigest()
    with pytest.raises(ConfigurationError):
        SQLiteDeepAgentsStore(store.state)
    assert hashlib.sha256(store.path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("field", ["deployment_id", "identity"])
def test_foreign_manifest_metadata_rejects_at_deployment_reader(store, field):
    import json

    with sqlite3.connect(store.path) as db:
        raw = db.execute("SELECT manifest_json FROM deployments").fetchone()[0]
        value = json.loads(raw)
        if field == "deployment_id":
            value[field] = str(uuid4())
        else:
            value["identity"]["agent_name"] = "foreign"
        db.execute("UPDATE deployments SET manifest_json=?", (canonical(value),))
    with pytest.raises(ConfigurationError):
        store.deployment()


def settle_waiting_customer(store, admission):
    from openeight.langchain.durable import CommandSettlement

    command = store.command(admission.thread_id, admission.command_id)
    thread = store.load(admission.thread_id)
    command = store.begin_command(
        thread.id, command.id, command.revision, thread.revision
    )
    thread = store.load(thread.id)
    return store.settle_command(
        thread.id,
        command.id,
        command.revision,
        CommandSettlement(
            "completed",
            thread.revision,
            "waiting_customer",
            str(uuid4()),
            ("saved output",),
        ),
    )


def test_duplicate_result_does_not_mix_old_output_with_later_thread_status(store):
    with store.acquire_deployment():
        admission = store.admit(kind="start", command_id="first", message="go")
        with store.acquire(admission.thread_id):
            settle_waiting_customer(store, admission)
        original = store.command_result(admission.thread_id, "first")
        assert original.status == "waiting_customer"
        reply = store.admit(
            kind="reply",
            command_id="second",
            thread_id=admission.thread_id,
            message="reply",
        )
        with store.acquire(reply.thread_id):
            command = store.command(reply.thread_id, reply.command_id)
            thread = store.load(reply.thread_id)
            store.begin_command(
                thread.id, command.id, command.revision, thread.revision
            )
            assert store.load(thread.id).status == "active"
        assert store.command_result(admission.thread_id, "first") == original


def test_command_snapshot_failure_rolls_back_command_and_visible_turns(store):
    with store.acquire_deployment():
        admission = store.admit(kind="start", command_id="first", message="go")
        with store.acquire(admission.thread_id):

            def fault(point):
                if point == "after_command_result_snapshot":
                    raise RuntimeError("injected snapshot failure")

            store.fault = fault
            with pytest.raises(RuntimeError):
                settle_waiting_customer(store, admission)
            command = store.command(admission.thread_id, "first")
            assert command.status == "running"
            with store._db() as db:
                assert (
                    db.execute("SELECT COUNT(*) FROM command_results").fetchone()[0]
                    == 0
                )
                assert (
                    db.execute(
                        "SELECT COUNT(*) FROM conversation_turns WHERE speaker='agent'"
                    ).fetchone()[0]
                    == 0
                )


@pytest.mark.parametrize("disposition", ["waiting_customer", "waiting"])
def test_recovered_resume_and_original_replay_exact_visible_result(store, disposition):
    from openeight.langchain.durable import CommandSettlement

    with store.acquire_deployment():
        admission = store.admit(kind="start", command_id="original", message="go")
        with store.acquire(admission.thread_id):
            command = store.command(admission.thread_id, "original")
            thread = store.load(admission.thread_id)
            store.begin_command(
                thread.id, command.id, command.revision, thread.revision
            )
        store.admit(kind="resume", command_id="resume-1", thread_id=admission.thread_id)
        with store.acquire(admission.thread_id):
            command = store.command(admission.thread_id, "resume-1")
            thread = store.load(admission.thread_id)
            command = store.begin_command(
                thread.id, command.id, command.revision, thread.revision
            )
            original = store.command(thread.id, "original")
            thread = store.load(thread.id)
            status = "waiting" if disposition == "waiting" else "completed"
            store.settle_command(
                thread.id,
                command.id,
                command.revision,
                CommandSettlement(
                    status,
                    thread.revision,
                    disposition,
                    str(uuid4()),
                    (),
                    recovered_command_id=original.id,
                    recovered_command_revision=original.revision,
                    recovered_status=status,
                    recovered_turns=("recovered answer",),
                ),
            )
        result = store.command_result(admission.thread_id, "resume-1")
        assert result.status == disposition and result.output == ("recovered answer",)
        assert store.command_result(admission.thread_id, "original") == result
        if disposition == "waiting":
            assert (
                store.command(admission.thread_id, "original").reason_code
                == "interrupted"
            )


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_read_only_schema_inspection_rejects_symlink_sidecars_before_sqlite(
    tmp_path, monkeypatch, suffix
):
    store = SQLiteDeepAgentsStore(tmp_path)
    target = tmp_path / "foreign"
    target.write_text("untouched")
    target.chmod(0o600)
    sidecar = tmp_path / (store.path.name + suffix)
    sidecar.symlink_to(target)

    def unexpected_open(*args, **kwargs):
        pytest.fail("SQLite must not inspect through an unvalidated sidecar")

    monkeypatch.setattr(sqlite3, "connect", unexpected_open)
    with pytest.raises(LocalError):
        store._inspect_existing()
    assert target.read_text() == "untouched"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        "revision",
        "compatibility",
        "topology",
        "configuration",
        "advanced_unverified",
        "local_verified",
    ],
)
async def test_invalid_initial_runtime_is_rejected_before_catalog_or_saver_access(
    tmp_path, monkeypatch, invalid
):
    import json
    from types import SimpleNamespace

    from openeight.deepagents import _admission
    from openeight.deepagents._deployment import credential_binding

    store = SQLiteDeepAgentsStore(tmp_path)
    identity = dict(IDENTITY)
    with store.acquire_deployment():
        deployment = store.initialize_enrollment(
            canonical(identity), credential_binding("key")
        )
    current = json.loads(manifest(deployment))
    monkeypatch.setattr(_admission, "_runtime_packages", lambda: current["packages"])
    owner = SimpleNamespace(
        _ensure_open=lambda: None,
        _integration="deepagents",
        control=SimpleNamespace(durable=SimpleNamespace(store=store)),
        binding=(IDENTITY["agent_name"], IDENTITY["endpoint"], "key"),
        _local_deployment=SimpleNamespace(
            revision="test-release", configuration_sha256="a" * 64
        ),
        _durable_deployment=None,
        _compatibility_selection=SimpleNamespace(**current["compatibility"]),
        _composition={
            "topology": current["topology"],
            "backend": current["backend"],
            "saver": current["saver"],
            "catalog_naming": current["catalog_naming"],
            "configuration_sha256": "a" * 64,
        },
    )
    if invalid == "local_verified":
        from dataclasses import replace

        verified = {**identity, "kind": "verified_issuance"}
        for field in ("organization_id", "workspace_id", "agent_id", "api_key_id"):
            verified[field] = "11111111-1111-4111-8111-111111111111"
        foreign = replace(deployment, identity_json=canonical(verified))
        owner.control.durable.store = SimpleNamespace(deployment=lambda: foreign)
    elif invalid == "advanced_unverified":
        owner._local_deployment = None
        owner._durable_deployment = SimpleNamespace(
            deployment_id=deployment.deployment_id,
            application_build_sha256="a" * 64,
            configuration_sha256="a" * 64,
        )
    elif invalid == "revision":
        owner._local_deployment.revision = "\x00"
    elif invalid == "compatibility":
        owner._compatibility_selection.implementation_revision = True
    elif invalid == "topology":
        owner._composition["topology"][0]["delegates_to"] = [["missing"]]
    else:
        owner._composition["configuration_sha256"] = "b" * 64
    # No catalog, client, or graph exists: malformed local facts must fail first.
    with pytest.raises(ConfigurationError) as caught:
        await _admission._validate_durable_admission(owner, bind=True)
    assert caught.value.reason == "durable_deployment_mismatch"
    assert store.deployment().manifest_json is None
