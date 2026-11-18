"""Destination persistence is separate from SDK openeight and checkpoint storage."""

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from contracts import Mutation
from destination import IncidentDestination
from simulator import BusinessError, canonical


def destination(tmp_path):
    value = IncidentDestination(tmp_path / "business")
    value.initialize(scenario="rollback", revision="release-1")
    return value


def mutation():
    return Mutation(
        str(uuid4()),
        "checkout",
        "rollback_release",
        0,
        canonical(dict(target_release="v41")),
    )


def test_destination_replays_exact_receipt_after_reopen_and_rejects_changed_request(
    tmp_path,
):
    store = destination(tmp_path)
    operation, key = mutation(), str(uuid4())
    first = store.apply_keyed(operation, idempotency_key=key)
    reopened = IncidentDestination(store.directory)
    assert reopened.apply_keyed(operation, idempotency_key=key) == first
    assert reopened.lookup(key) == first
    assert reopened.snapshot().revision == 1 and reopened.snapshot().release == "v41"
    assert reopened.effects() == (first,)
    for request, action_key in (
        (replace(operation, expected_revision=1), key),
        (operation, str(uuid4())),
    ):
        with pytest.raises(BusinessError) as caught:
            reopened.apply_keyed(request, idempotency_key=action_key)
        assert caught.value.code == "action_conflict"
    assert reopened.snapshot().revision == 1


@pytest.mark.parametrize(
    "boundary,committed",
    [
        ("after_service_update", False),
        ("after_destination_receipt", False),
        ("after_destination_commit", True),
    ],
)
def test_process_death_never_separates_service_change_from_receipt(
    tmp_path, boundary, committed
):
    store = destination(tmp_path)
    operation, key = mutation(), str(uuid4())
    script = """
import json,os,sys
from contracts import Mutation
from destination import IncidentDestination
request=json.load(sys.stdin)
def fault(point):
    if point==request['boundary']:os._exit(71)
store=IncidentDestination(request['directory'],fault=fault)
store.apply_keyed(Mutation(**request['mutation']),idempotency_key=request['key'])
"""
    from dataclasses import asdict

    result = subprocess.run(
        [sys.executable, "-P", "-c", script],
        input=json.dumps(
            dict(
                directory=str(store.directory),
                boundary=boundary,
                key=key,
                mutation=asdict(operation),
            )
        ),
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        timeout=10,
    )
    assert result.returncode == 71
    reopened = IncidentDestination(store.directory)
    receipt = reopened.lookup(key)
    assert bool(receipt) is committed
    assert reopened.snapshot().revision == int(committed)
    completed = reopened.apply_keyed(operation, idempotency_key=key)
    assert reopened.lookup(key) == completed and reopened.snapshot().revision == 1
    assert len(reopened.effects()) == 1


def test_destination_refuses_changed_configuration_and_foreign_or_insecure_state(
    tmp_path,
):
    store = destination(tmp_path)
    store.initialize(scenario="rollback", revision="release-1")
    with pytest.raises(BusinessError) as caught:
        store.initialize(scenario="rollback", revision="changed")
    assert caught.value.code == "destination_configuration_mismatch"
    foreign = tmp_path / "foreign"
    foreign.mkdir(mode=0o700)
    database = foreign / "incident.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE unrelated(value TEXT)")
    database.chmod(0o600)
    before = database.read_bytes()
    with pytest.raises(BusinessError) as caught:
        IncidentDestination(foreign).initialize(
            scenario="rollback", revision="release-1"
        )
    assert (
        caught.value.code == "unsupported_destination"
        and database.read_bytes() == before
    )
    store.path.chmod(0o644)
    with pytest.raises(BusinessError) as caught:
        store.snapshot()
    assert caught.value.code == "insecure_destination"


def test_destination_rejects_symlink_and_records_only_exact_canonical_report(tmp_path):
    store = destination(tmp_path)
    linked = tmp_path / "linked"
    linked.symlink_to(store.directory, target_is_directory=True)
    with pytest.raises(BusinessError):
        IncidentDestination(linked).snapshot()
    report = canonical(
        dict(schema_version=1, service_id="checkout", observed_revision=0, actions=[])
    )
    assert not store.has_report(report)
    store.record_report(report)
    store.record_report(report)
    assert IncidentDestination(store.directory).has_report(report)
    with pytest.raises(BusinessError):
        store.record_report(json.dumps(json.loads(report)))
