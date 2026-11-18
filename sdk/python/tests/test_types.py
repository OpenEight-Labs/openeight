"""Runtime JSON bounds shared by Event and selected policy input."""

import pytest
import json
from pathlib import Path
from openeight import Event, ConfigurationError


@pytest.mark.parametrize(
    "value", ["x" * 65535, {"x" * 65535: 1}], ids=["string", "key"]
)
def test_event_rejects_oversized_json_tokens(value):
    with pytest.raises(ConfigurationError) as error:
        Event(
            name="call",
            observation_kind="span",
            operation_kind="invoke",
            occurred_at="2026-09-01T12:00:00Z",
            outcome="success",
            input=value,
        )
    assert error.value.reason == "invalid_event"
    assert error.value.__cause__ is None


def test_error_event_with_absent_output_omits_output_json_from_wire():
    event = Event(
        name="call",
        observation_kind="span",
        operation_kind="invoke",
        occurred_at="2026-09-01T12:00:00Z",
        outcome="error",
        error={"kind": "agent_error"},
    )

    write = event._as_write()

    assert "output_json" not in write
    assert write["error_json"] == '{"kind":"agent_error"}'


def test_success_event_keeps_output_json_on_wire():
    event = Event(
        name="call",
        observation_kind="span",
        operation_kind="invoke",
        occurred_at="2026-09-01T12:00:00Z",
        outcome="success",
    )

    assert event._as_write()["output_json"] == "null"


@pytest.mark.parametrize(
    "vector",
    json.loads(Path(__file__).with_name("policy_input_vectors.json").read_text()),
    ids=lambda v: v["name"],
)
def test_shared_runtime_json_vectors(vector):
    from openeight._json import snapshot_json

    if "raw" in vector:
        value = json.loads(vector["raw"])
    elif "depth" in vector:
        nested = 0
        for _ in range(vector["depth"] - 1):
            nested = [nested]
        value = {"value": nested}
    elif "token_bytes" in vector:
        value = {"value": "x" * (vector["token_bytes"] - 2)}
    else:
        items = ["x" * 65000] * 16 + [""]
        items[-1] = "x" * (
            vector["document_bytes"]
            - len(json.dumps({"items": items}, separators=(",", ":")))
        )
        value = {"items": items}
    if vector["valid"]:
        assert (
            json.loads(snapshot_json(value, "invalid_input", object_root=True)) == value
        )
    else:
        with pytest.raises(ConfigurationError):
            snapshot_json(value, "invalid_input", object_root=True)
