"""Direct telemetry rejects invalid records before taking queue ownership."""

import json

import httpx
import pytest
from test_errors import _assert_exported_error_has_no_unsafe_sdk_traceback_state

from openeight import Client, ConfigurationError, Event

RUN = "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b12"
EVENT = "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b13"
TOOL = "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b14"
EDGE = "6ba7b810-9dad-51d1-80b4-00c04fd430c8"
TIME = "2026-09-01T00:00:00Z"
REGISTERED = {"tool_id": TOOL, "tool_version_id": TOOL, "tool_name": "example"}
BASE = {
    "id": EVENT,
    "run_id": RUN,
    "name": "example",
    "observation_kind": "span",
    "operation_kind": "invoke",
    "outcome": "success",
    "occurred_at": TIME,
}


def client(writes):
    def handler(request):
        body = json.loads(request.content)
        writes.append(body)
        return httpx.Response(
            200,
            json={
                key: [{"id": row["id"], "replayed": False} for row in body.get(key, [])]
                for key in ("runs", "events")
            },
        )

    return Client(
        "https://example.com",
        api_key="ctk_admission_canary",
        agent_name="example",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize(
    "field", ["status", "observation_kind", "operation_kind", "outcome", "span_role"]
)
@pytest.mark.parametrize("value", [[], {}, ["success"], {"value": "span"}, 0, False])
def test_malformed_enums_export_safe_configuration_errors(field, value):
    writes = []
    with client(writes) as c:
        if field == "status":
            args = {"runs": [{"id": RUN, "status": value, "started_at": TIME}]}
        else:
            args = {"events": [{**BASE, field: value}]}
        with pytest.raises(ConfigurationError) as caught:
            c.write_telemetry(**args)
        _assert_exported_error_has_no_unsafe_sdk_traceback_state(
            caught.value, {"ctk_admission_canary"}
        )
    assert writes == []


@pytest.mark.parametrize("field", ["name", "extension"])
def test_invalid_unicode_is_rejected_without_retaining_client(field):
    writes = []
    with client(writes) as c:
        with pytest.raises(ConfigurationError) as caught:
            c.write_telemetry(events=[{**BASE, field: "\ud800"}])
        _assert_exported_error_has_no_unsafe_sdk_traceback_state(
            caught.value, {"ctk_admission_canary"}
        )
    assert writes == []


INVALID = [
    {"error_json": "null"},
    {"outcome": "error"},
    {"outcome": "error", "error_json": "null", "output_json": "null"},
    {"outcome": "error", "error_json": "{}", "output_json": "{}"},
    {"outcome": "interrupted", "output_json": " null "},
    {"outcome": "interrupted", "error_json": "null"},
    *(
        {
            "observation_kind": "lifecycle",
            "operation_kind": "unspecified",
            "outcome": "interrupted",
            field: "null",
        }
        for field in ("input_json", "output_json", "error_json")
    ),
    *(
        {"observation_kind": kind, **identity}
        for kind in ("span", "llm_call")
        for identity in ({"tool_name": "example"}, REGISTERED, {"decision_id": TOOL})
    ),
    {"observation_kind": "tool_call"},
    {"observation_kind": "tool_call", "tool_id": TOOL, "tool_name": "example"},
    {
        "observation_kind": "tool_call",
        "tool_name": "example",
        "decision_id": EDGE,
        "input_json": "{}",
    },
    {"observation_kind": "tool_call", **REGISTERED, "decision_id": EDGE},
    {
        "observation_kind": "tool_call",
        **REGISTERED,
        "decision_id": EDGE,
        "input_json": "null",
    },
    {
        "observation_kind": "tool_call",
        **REGISTERED,
        "decision_id": EDGE,
        "input_json": "[]",
    },
    {
        "observation_kind": "tool_call",
        **REGISTERED,
        "decision_id": EDGE,
        "input_json": "{}",
        "outcome": "unspecified",
    },
    {
        "observation_kind": "lifecycle",
        "operation_kind": "unspecified",
        "outcome": "interrupted",
        "tool_name": "example",
    },
]


@pytest.mark.parametrize("patch", INVALID)
def test_invalid_payload_and_identity_combinations_never_dispatch(patch):
    writes = []
    with client(writes) as c:
        with pytest.raises(ConfigurationError):
            c.write_telemetry(events=[{**BASE, **patch}])
    assert writes == []


@pytest.mark.parametrize(
    "patch",
    [
        {"input_json": "null", "output_json": "null"},
        {"outcome": "error", "error_json": " null "},
        {"outcome": "interrupted"},
        {
            "observation_kind": "lifecycle",
            "operation_kind": "unspecified",
            "outcome": "interrupted",
        },
        {"observation_kind": "tool_call", "tool_name": "example"},
        {"observation_kind": "tool_call", **REGISTERED},
        {
            "observation_kind": "tool_call",
            **REGISTERED,
            "decision_id": EDGE,
            "parent_event_id": EDGE,
            "input_json": "{}",
        },
        {
            "observation_kind": "lifecycle",
            "operation_kind": "unspecified",
            "outcome": "interrupted",
            **REGISTERED,
            "decision_id": EDGE,
        },
        {
            "observation_kind": "llm_call",
            "parent_event_id": EDGE,
            "input_tokens": "9223372036854775807",
            "output_tokens": 0,
            "extension": {"safe": True},
        },
    ],
)
def test_valid_wire_presence_identity_and_metrics_remain_unchanged(patch):
    writes = []
    row = {**BASE, **patch}
    with client(writes) as c:
        c.write_telemetry(events=[row])
    assert writes[0]["events"][0] == row


@pytest.mark.parametrize(
    "kwargs",
    [
        {"observation_kind": "span", "outcome": "error", "error": {}, "output": {}},
        {"observation_kind": "span", "outcome": "success", **REGISTERED},
        {"observation_kind": "tool_call", "outcome": "success"},
    ],
)
def test_rejected_event_does_not_poison_queue_or_completion(kwargs):
    writes = []
    with client(writes) as c:
        run = c.governed_run(run_id=RUN, started_at=TIME)
        with pytest.raises(ConfigurationError):
            run.submit_event(
                Event(
                    name="example", operation_kind="invoke", occurred_at=TIME, **kwargs
                )
            )
        run.submit_event(Event("valid", "span", "invoke", TIME, "success"))
        run.close()
    events = [e for body in writes for e in body.get("events", [])]
    assert len(events) == 1 and events[0]["name"] == "valid"
    assert writes[-1]["runs"][0]["status"] == "completed"


@pytest.mark.parametrize(
    "patch", [{"errorJson": "{}"}, {"toolId": TOOL}, {"decisionId": TOOL}]
)
def test_protocol_aliases_cannot_bypass_event_validation(patch):
    writes = []
    with client(writes) as c:
        with pytest.raises(ConfigurationError):
            c.write_telemetry(events=[{**BASE, **patch}])
    assert writes == []


@pytest.mark.parametrize(
    "field", ["observation_kind", "operation_kind", "outcome", "span_role"]
)
def test_malformed_aliases_export_safe_errors(field):
    row = {**BASE, field: []}
    alias = field.split("_")[0] + "".join(part.title() for part in field.split("_")[1:])
    if alias != field:
        row[alias] = row.pop(field)
    writes = []
    with client(writes) as c:
        with pytest.raises(ConfigurationError) as caught:
            c.write_telemetry(events=[row])
        _assert_exported_error_has_no_unsafe_sdk_traceback_state(
            caught.value, {"ctk_admission_canary"}
        )
    assert writes == []


def test_valid_protocol_aliases_are_preserved_and_duplicates_rejected():
    writes = []
    row = {
        "id": EVENT,
        "runId": RUN,
        "name": "example",
        "observationKind": "tool_call",
        "operationKind": "invoke",
        "outcome": "error",
        "occurredAt": TIME,
        "errorJson": "null",
        "toolId": TOOL,
        "toolVersionId": TOOL,
        "toolName": "example",
        "decisionId": EDGE,
        "inputJson": "{}",
        "extension": {"safe": True},
    }
    run = {"id": RUN, "status": "active", "startedAt": TIME, "attributesJson": "{}"}
    with client(writes) as c:
        c.write_telemetry(runs=[run], events=[row])
        for event in [
            {**row, "run_id": RUN},
            {**BASE, "output_json": "null", "outputJson": "null"},
        ]:
            with pytest.raises(ConfigurationError):
                c.write_telemetry(events=[event])
    assert writes == [{"runs": [run], "events": [row]}]
