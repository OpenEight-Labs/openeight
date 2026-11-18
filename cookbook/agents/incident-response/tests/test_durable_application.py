"""Public SDK commands preserve incident receipts across owner restarts."""

from uuid import uuid4

import pytest
from openeight import ConfigurationError

from durable_cli import run_durable
from simulator import BusinessError
from test_managed import settings


def test_durable_rollback_reopens_and_replays_one_receipt(tmp_path, control):
    arguments = dict(
        directory=tmp_path / "incident", revision="cookbook-2", settings=settings()
    )
    initialized, code = run_durable(
        action="initialize", scenario="rollback", **arguments
    )
    assert code == 0, initialized
    command_id = str(uuid4())
    completed, code = run_durable(action="start", command_id=command_id, **arguments)
    assert code == 0, completed
    assert completed["report_validation"]["status"] == "passed"
    assert len(completed["effects"]) == 1
    repeated, code = run_durable(action="start", command_id=command_id, **arguments)
    assert code == 0, repeated
    assert repeated["effects"] == completed["effects"]
    assert repeated["execution"] == completed["execution"]
    with pytest.raises(BusinessError, match="destination_configuration_mismatch"):
        run_durable(
            action="start",
            command_id=str(uuid4()),
            **{**arguments, "revision": "changed"},
        )


def test_durable_denied_child_has_no_destination_effect(tmp_path, control):
    arguments = dict(
        directory=tmp_path / "incident", revision="cookbook-2", settings=settings()
    )
    initialized, code = run_durable(
        action="initialize", scenario="rollback", **arguments
    )
    assert code == 0, initialized
    control.deny_tool = next(
        row["tool_id"]
        for row in initialized["catalog"]
        if row["native_name"] == "rollback_release"
    )
    with pytest.raises(ConfigurationError) as stopped:
        run_durable(action="start", command_id=str(uuid4()), **arguments)
    assert stopped.value.reason == "durable_reconciliation_required"
    stopped, code = run_durable(action="inspect", **arguments)
    assert code == 0
    assert stopped["effects"] == []
    assert stopped["state"]["release"] == "v42"
