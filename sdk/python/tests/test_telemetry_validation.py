import json
import math

import httpx
import pytest

from openeight import (
    BufferedObserveWriter,
    Client,
    ConfigurationError,
    ObserveDeliveryError,
    ObserveQueueFullError,
    ObserveRunCompletedError,
    ProtocolError,
    telemetry,
)
from openeight._forbidden_fields import FORBIDDEN_TELEMETRY_SELECTORS


def _run(id):
    return {"id": id, "status": "active", "started_at": "2026-09-01T12:00:00Z"}


def _event(id, run_id):
    return {
        "id": id,
        "run_id": run_id,
        "name": "example",
        "observation_kind": "span",
        "operation_kind": "invoke",
        "outcome": "success",
        "occurred_at": "2026-09-01T12:00:00Z",
    }


def _client(requests):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                field: [{"id": write["id"], "replayed": False} for write in body[field]]
                for field in ("runs", "events")
            },
        )

    return Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(handler),
    )


def _assert_local_rejection(client, requests, reason, call):
    with pytest.raises(ConfigurationError) as caught:
        call()
    assert caught.value.reason == reason
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert requests == []


def test_buffered_writer_uses_public_safe_error_hierarchy_without_causes():
    class SenderCanary(Exception):
        pass

    def fail_delivery(*_):
        raise SenderCanary("raw-provider-canary")

    assert telemetry.ObserveDeliveryError is ObserveDeliveryError
    assert telemetry.ObserveQueueFullError is ObserveQueueFullError
    assert telemetry.ObserveRunCompletedError is ObserveRunCompletedError

    writer = BufferedObserveWriter(
        fail_delivery,
        flush_records=100,
        flush_interval=60,
    )
    run = writer.begin_run()
    run.submit({"name": "queued"})
    with pytest.raises(ObserveDeliveryError) as caught:
        writer.flush()
    assert caught.value.reason == "delivery_failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "raw-provider-canary" not in str(caught.value) + repr(caught.value)

    bounded = BufferedObserveWriter(lambda *_: None, max_records=1)
    bounded.begin_run()
    with pytest.raises(ObserveQueueFullError) as caught:
        bounded.begin_run()
    assert caught.value.reason == "run_registry_full"

    completed = BufferedObserveWriter(lambda *_: None).begin_run()
    completed.submit({"name": "terminal"}, completed=True)
    with pytest.raises(ObserveRunCompletedError) as caught:
        completed.submit({"name": "late"})
    assert caught.value.reason == "run_completed"


def test_buffered_writer_close_replays_fresh_safe_descriptor_only():
    def fail_delivery(*_):
        raise RuntimeError("retained-provider-canary")

    writer = BufferedObserveWriter(
        fail_delivery,
        flush_records=100,
        flush_interval=60,
    )
    writer.begin_run().submit({"name": "queued"})

    failures = []
    for _ in range(2):
        with pytest.raises(ObserveDeliveryError) as caught:
            writer.close()
        failures.append(caught.value)
        assert caught.value.reason == "delivery_failed"
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        assert "retained-provider-canary" not in str(caught.value) + repr(caught.value)

    assert failures[0] is not failures[1]

    with pytest.raises(ObserveDeliveryError) as caught:
        writer.close(timeout=0)
    assert caught.value.reason == "delivery_failed"


@pytest.mark.parametrize("close_fails", [False, True], ids=["closed", "failed-close"])
def test_closed_writer_rejects_public_flush_and_context_reentry(close_fails):
    def send(*_):
        if close_fails:
            raise RuntimeError("provider-close-canary")

    writer = BufferedObserveWriter(send, flush_records=100, flush_interval=60)
    if close_fails:
        writer.begin_run().submit({"name": "queued"})
        with pytest.raises(ObserveDeliveryError):
            writer.close()
    else:
        writer.close()

    with pytest.raises(ConfigurationError) as flush_error:
        writer.flush()
    assert flush_error.value.reason == "writer_closed"

    with pytest.raises(ConfigurationError) as enter_error:
        writer.__enter__()
    assert enter_error.value.reason == "writer_closed"


def test_private_forbidden_selector_catalog_is_exact_and_not_exported():
    import openeight

    assert FORBIDDEN_TELEMETRY_SELECTORS == {
        "organization_id",
        "workspace_id",
        "agent_id",
        "api_key_id",
        "credential_id",
        "policy_id",
        "policy_version_id",
        "tool_version",
    }
    assert "FORBIDDEN_TELEMETRY_SELECTORS" not in openeight.__all__
    assert not hasattr(openeight, "FORBIDDEN_TELEMETRY_SELECTORS")


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("runs", False, "invalid_runs"),
        ("runs", 0, "invalid_runs"),
        ("runs", "", "invalid_runs"),
        ("runs", {}, "invalid_runs"),
        ("runs", (), "invalid_runs"),
        ("events", False, "invalid_events"),
        ("events", 0, "invalid_events"),
        ("events", "", "invalid_events"),
        ("events", {}, "invalid_events"),
        ("events", (), "invalid_events"),
    ],
)
def test_raw_telemetry_requires_actual_list_containers(field, value, reason):
    requests = []
    client = _client(requests)
    try:
        kwargs = {"runs": [], "events": [], field: value}
        _assert_local_rejection(
            client,
            requests,
            reason,
            lambda: client.write_telemetry(**kwargs),
        )
    finally:
        client.close()


@pytest.mark.parametrize(
    ("field", "entry", "reason"),
    [
        ("runs", None, "invalid_runs"),
        ("runs", False, "invalid_runs"),
        ("runs", [], "invalid_runs"),
        ("runs", "", "invalid_runs"),
        ("events", None, "invalid_events"),
        ("events", False, "invalid_events"),
        ("events", [], "invalid_events"),
        ("events", "", "invalid_events"),
    ],
)
def test_raw_telemetry_requires_json_object_entries(field, entry, reason):
    requests = []
    client = _client(requests)
    try:
        kwargs = {"runs": [], "events": [], field: [entry]}
        _assert_local_rejection(
            client,
            requests,
            reason,
            lambda: client.write_telemetry(**kwargs),
        )
    finally:
        client.close()


@pytest.mark.parametrize(
    ("field", "entry", "reason"),
    [
        ("runs", {"value": math.nan}, "invalid_runs"),
        ("runs", {"value": object()}, "invalid_runs"),
        ("runs", {1: "non-string-key"}, "invalid_runs"),
        pytest.param(
            "runs",
            {"value": 10**10_000},
            "invalid_runs",
            id="runs-unencodable-integer",
        ),
        ("events", {"value": math.inf}, "invalid_events"),
        ("events", {"value": object()}, "invalid_events"),
        ("events", {1: "non-string-key"}, "invalid_events"),
        pytest.param(
            "events",
            {"value": 10**10_000},
            "invalid_events",
            id="events-unencodable-integer",
        ),
    ],
)
def test_raw_telemetry_requires_strict_json_objects(field, entry, reason):
    requests = []
    client = _client(requests)
    try:
        kwargs = {"runs": [], "events": [], field: [entry]}
        _assert_local_rejection(
            client,
            requests,
            reason,
            lambda: client.write_telemetry(**kwargs),
        )
    finally:
        client.close()


@pytest.mark.parametrize(
    "selector",
    [
        "organization_id",
        "workspace_id",
        "agent_id",
        "api_key_id",
        "credential_id",
        "policy_id",
        "policy_version_id",
        "tool_version",
    ],
)
@pytest.mark.parametrize("field", ["runs", "events"])
def test_raw_telemetry_rejects_top_level_authority_and_persistence_selectors(
    field,
    selector,
):
    requests = []
    client = _client(requests)
    try:
        kwargs = {"runs": [], "events": [], field: [{selector: "server-id-canary"}]}
        _assert_local_rejection(
            client,
            requests,
            "invalid_telemetry_scope",
            lambda: client.write_telemetry(**kwargs),
        )
    finally:
        client.close()


def test_raw_telemetry_allows_forbidden_names_only_inside_nested_application_data():
    requests = []
    client = _client(requests)
    try:
        client.write_telemetry(
            runs=[
                {
                    **_run("018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b12"),
                    "attributes_json": '{"agent_id":"inert-application-data"}',
                }
            ],
            events=[
                {
                    **_event(
                        "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b13",
                        "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b12",
                    ),
                    "attributes_json": '{"workspace_id":"inert-application-data"}',
                }
            ],
        )
    finally:
        client.close()


def test_raw_telemetry_success_discards_wire_correlation_results():
    run_id = "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b12"
    event_id = "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b13"

    def handler(_request):
        return httpx.Response(
            200,
            json={
                "runs": [{"id": run_id, "replayed": False}],
                "events": [{"id": event_id, "replayed": True}],
            },
        )

    client = Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = client.write_telemetry(
            runs=[_run(run_id)],
            events=[_event(event_id, run_id)],
        )
    finally:
        client.close()

    assert result is None


def test_raw_telemetry_accepts_acknowledgements_in_any_order():
    run_ids = [
        "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b12",
        "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b14",
    ]

    def handler(_request):
        return httpx.Response(
            200,
            json={
                "runs": [
                    {"id": run_ids[1], "replayed": True},
                    {"id": run_ids[0], "replayed": False},
                ],
                "events": [],
            },
        )

    client = Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(handler),
    )
    try:
        client.write_telemetry(
            runs=[_run(run_ids[0]), _run(run_ids[1])],
            events=[],
        )
    finally:
        client.close()


@pytest.mark.parametrize(
    "response_body",
    [
        {"runs": {}, "events": []},
        {"runs": [], "events": [], "agent_id": "server-agent-id"},
        {"runs": [{"id": "server-id", "replayed": False}], "events": []},
        {
            "runs": [{"id": "run", "replayed": False, "agent_id": "server-agent-id"}],
            "events": [],
        },
    ],
)
def test_raw_telemetry_rejects_malformed_or_unexpected_id_responses(response_body):
    client = Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=response_body)
        ),
    )
    try:
        with pytest.raises(ProtocolError) as caught:
            client.write_telemetry(runs=[], events=[])
    finally:
        client.close()

    assert caught.value.status_code == 200
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "server-agent-id" not in repr(caught.value)


@pytest.mark.parametrize(
    "acknowledgements",
    [
        [],
        [
            {"id": "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b12", "replayed": False},
            {"id": "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b12", "replayed": False},
        ],
        [
            {"id": "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b12", "replayed": False},
            {"id": "run-extra", "replayed": False},
        ],
        [
            {"id": "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b12", "replayed": 0},
            {"id": "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b14", "replayed": False},
        ],
        [
            {
                "id": "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b12",
                "replayed": False,
                "agent_id": "server-owned",
            },
            {"id": "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b14", "replayed": False},
        ],
    ],
    ids=["missing", "duplicate", "extra", "non-boolean-replayed", "extra-field"],
)
def test_raw_telemetry_rejects_nonexact_acknowledgement_sets(acknowledgements):
    client = Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"runs": acknowledgements, "events": []},
            )
        ),
    )
    try:
        with pytest.raises(ProtocolError) as caught:
            client.write_telemetry(
                runs=[
                    _run("018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b12"),
                    _run("018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b14"),
                ],
                events=[],
            )
    finally:
        client.close()

    assert caught.value.status_code == 200


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"send": None}, "invalid_observe_sender"),
        ({"send_completed": False}, "invalid_completed_sender"),
        ({"encode_request": False}, "invalid_observe_encoder"),
        ({"max_records": 0}, "invalid_writer_limit"),
        ({"max_bytes": False}, "invalid_writer_limit"),
        ({"flush_records": -1}, "invalid_writer_limit"),
        ({"flush_bytes": 1.5}, "invalid_writer_limit"),
        ({"flush_interval": math.nan}, "invalid_writer_limit"),
        ({"max_request_records": 1}, "invalid_writer_limit"),
        ({"max_request_bytes": 0}, "invalid_writer_limit"),
        (
            {
                "max_records": 1,
                "flush_records": 2,
                "max_bytes": 1,
                "flush_bytes": 2,
            },
            "invalid_writer_threshold",
        ),
    ],
)
def test_buffered_writer_rejects_invalid_configuration_with_safe_typed_errors(
    kwargs,
    reason,
):
    options = {"send": lambda *_: None, **kwargs}
    with pytest.raises(ConfigurationError) as caught:
        BufferedObserveWriter(**options)

    assert caught.value.reason == reason
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "event",
    [
        ["not-an-object"],
        {"value": object()},
        {"recursive": None},
    ],
)
def test_buffered_writer_maps_event_freezing_failures_to_configuration_error(event):
    if isinstance(event, dict) and "recursive" in event:
        event["recursive"] = event
    writer = BufferedObserveWriter(lambda *_: None)
    run = writer.begin_run()

    with pytest.raises(ConfigurationError) as caught:
        run.submit(event)

    assert caught.value.reason == "invalid_event"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
