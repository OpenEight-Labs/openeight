"""Malformed or foreign receipts cannot settle an executing generated attempt."""

import copy
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("deepagents")

from test_local_durable_store import bound_store, prepare

from openeight import ConfigurationError
from openeight.deepagents._frameworks._state import (
    MAX_RECEIPT_BYTES,
    decode_state_update,
    encode_state_update,
)
from openeight.deepagents._local_durable._store import SQLiteDeepAgentsStore

FILE = {
    "content": "original",
    "encoding": "utf-8",
    "created_at": "2026-11-04T00:00:00Z",
    "modified_at": "2026-11-04T00:00:00Z",
}


@pytest.fixture
def store(tmp_path):
    return bound_store(tmp_path)


@pytest.mark.parametrize(
    "update",
    [
        {"messages": []},
        {"files": []},
        {"files": {"relative": FILE}},
        {"files": {"//host/file": FILE}},
        {"files": {"/a/../file": FILE}},
        {"files": {"/a/./file": FILE}},
        {"files": {"/a//file": FILE}},
        {"files": {1: FILE}},
        {"files": {"/file": {"content": "missing fields"}}},
        {"files": {"/file": {**FILE, "unexpected": "field"}}},
        {"files": {"/file": {**FILE, "encoding": "base64"}}},
        {"files": {"/file": {**FILE, "content": b"bytes"}}},
        {"files": {"/file": {**FILE, "created_at": "not-a-date"}}},
        {"files": {"/file": {**FILE, "created_at": "2026-11-04T00:00:00"}}},
        {"files": {"/file": {**FILE, "modified_at": "2026-11-04T00:00:00+01:00"}}},
        {"todos": {}},
        {"todos": [{"content": "work", "status": "unrecognized"}]},
        {"todos": [{"content": "work", "status": []}]},
        {"todos": [{"content": 1, "status": "pending"}]},
        {"todos": [{"content": "work", "status": "pending", "extra": True}]},
        {"todos": [{"status": "pending"}]},
        {"files": {"/file": {**FILE, "content": "\ud800"}}},
    ],
)
def test_unknown_or_malformed_updates_require_reconciliation(update):
    with pytest.raises(ConfigurationError) as caught:
        encode_state_update(update)
    assert caught.value.reason == "durable_reconciliation_required"


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        " {}",
        '{"files":{},"files":{}}',
        '{"todos":[],"files":{}}',
        '{"files":{"/same":null,"/same":null}}',
        "[" * 2000 + "]" * 2000,
    ],
)
def test_noncanonical_duplicate_or_deep_updates_require_reconciliation(raw):
    with pytest.raises(ConfigurationError) as caught:
        decode_state_update(raw)
    assert caught.value.reason == "durable_reconciliation_required"


def test_receipt_byte_bound_is_utf8_and_valid_state_round_trips_without_aliasing():
    value = {
        "files": {"/report.txt": copy.deepcopy(FILE)},
        "todos": [
            {"content": "investigate", "status": "in_progress"},
            {"content": "done", "status": "completed"},
        ],
    }
    raw = encode_state_update(value)
    decoded = decode_state_update(raw)
    assert decoded == value
    decoded["files"]["/report.txt"]["content"] = "mutated later"
    assert decode_state_update(raw) == value
    empty = {"files": {"/report.txt": {**FILE, "content": ""}}}
    overhead = len(encode_state_update(empty).encode())
    empty["files"]["/report.txt"]["content"] = "x" * (MAX_RECEIPT_BYTES - overhead)
    assert len(encode_state_update(empty).encode()) == MAX_RECEIPT_BYTES
    empty["files"]["/report.txt"]["content"] += "x"
    with pytest.raises(ConfigurationError):
        encode_state_update(empty)
    empty["files"]["/report.txt"]["content"] = "🙂" * (MAX_RECEIPT_BYTES // 4)
    with pytest.raises(ConfigurationError):
        encode_state_update(empty)


@pytest.mark.parametrize("field", ["deployment_id", "thread_id", "decision_id"])
def test_foreign_scoped_receipt_never_settles_or_enqueues(store, field):
    with store.acquire_deployment():
        admission = store.admit(kind="start", command_id="stable-start", message="go")
        with store.acquire(admission.thread_id):
            attempt, _, settlement, receipt = prepare(store)
            before = store.pending(attempt.thread_id)
            with pytest.raises(ConfigurationError):
                store.settle_generated_attempt(
                    attempt.thread_id,
                    attempt.tool_call_id,
                    attempt.revision,
                    settlement,
                    replace(receipt, **{field: str(uuid4())}),
                )
            assert store.get_attempt(attempt.thread_id, attempt.tool_call_id) == attempt
            assert (
                store.generated_state_receipt(attempt.thread_id, attempt.tool_call_id)
                is None
            )
            assert store.pending(attempt.thread_id) == before


@pytest.mark.parametrize("version", [None, False, 0, 2, 1.0, "1"])
def test_unrecognized_receipt_format_cannot_commit_partial_settlement(store, version):
    with store.acquire_deployment():
        admission = store.admit(kind="start", command_id="stable-start", message="go")
        with store.acquire(admission.thread_id):
            attempt, _, settlement, receipt = prepare(store)
            with pytest.raises(ConfigurationError):
                store.settle_generated_attempt(
                    attempt.thread_id,
                    attempt.tool_call_id,
                    attempt.revision,
                    settlement,
                    replace(receipt, format_version=version),
                )
            assert store.get_attempt(attempt.thread_id, attempt.tool_call_id) == attempt
            assert (
                store.generated_state_receipt(attempt.thread_id, attempt.tool_call_id)
                is None
            )
            assert not [
                r for r in store.pending(attempt.thread_id) if r.kind == "event"
            ]


def _child_environment():
    return {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).parent)
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    }


def test_two_live_processes_cannot_own_the_same_deployment_and_thread(store):
    with store.acquire_deployment():
        admission = store.admit(kind="start", command_id="stable-start", message="go")
    code = """
import sys
from openeight.deepagents._local_durable._store import SQLiteDeepAgentsStore
store=SQLiteDeepAgentsStore(sys.argv[1])
with store.acquire_deployment(), store.acquire(sys.argv[2]):
    print('held',flush=True)
    sys.stdin.readline()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(store.state), admission.thread_id],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_environment(),
    )
    try:
        assert child.stdout.readline().strip() == "held"
        assert child.poll() is None
        with pytest.raises(ConfigurationError) as caught:
            with store.acquire_deployment(), store.acquire(admission.thread_id):
                pytest.fail("a second process acquired the live thread")
        assert caught.value.reason == "integration_busy"
        assert child.poll() is None
    finally:
        child.communicate("release\n", timeout=10)
    assert child.returncode == 0
    with store.acquire_deployment(), store.acquire(admission.thread_id):
        assert store.load(admission.thread_id).id == admission.thread_id


@pytest.mark.parametrize(
    "point",
    ["after_generated_receipt_insert", "after_attempt_result", "after_attempt_outbox"],
)
def test_real_process_death_inside_settlement_rolls_back_receipt_result_and_event(
    store, point
):
    marker = store.state.parent / (store.state.name + "-interrupted.json")
    code = """
import json,os,sys
from pathlib import Path
from test_local_durable_store import prepare
from openeight.deepagents._local_durable._store import SQLiteDeepAgentsStore
store=SQLiteDeepAgentsStore(sys.argv[1])
with store.acquire_deployment():
    admission=store.admit(kind='start',command_id='stable-start',message='go')
    with store.acquire(admission.thread_id):
        attempt,_,settlement,receipt=prepare(store)
        Path(sys.argv[3]).write_text(json.dumps({'thread':attempt.thread_id,'key':attempt.tool_call_id}))
        def fault(point):
            if point==sys.argv[2]: os._exit(83)
        store.fault=fault
        store.settle_generated_attempt(attempt.thread_id,attempt.tool_call_id,attempt.revision,settlement,receipt)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(store.state), point, str(marker)],
        env=_child_environment(),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 83, result.stderr
    identity = json.loads(marker.read_text())
    reopened = SQLiteDeepAgentsStore(store.state)
    with reopened.acquire_deployment(), reopened.acquire(identity["thread"]):
        attempt = reopened.get_attempt(identity["thread"], identity["key"])
        assert attempt.status == "executing" and attempt.result_json is None
        assert (
            reopened.generated_state_receipt(identity["thread"], identity["key"])
            is None
        )
        assert not [
            r for r in reopened.pending(identity["thread"]) if r.kind == "event"
        ]
