from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone
import inspect

import httpx
import pytest

import openeight
from openeight import Client, ConfigurationError, DecisionResult, Event, ProtocolError


DECISION_ID = "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b11"
RUN_ID = "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b12"


def test_root_exports_retain_observe_writer_and_run_handle():
    assert "BufferedObserveWriter" in openeight.__all__
    assert "ObserveRunHandle" in openeight.__all__
    assert openeight.BufferedObserveWriter.__module__ == "openeight.telemetry"
    assert openeight.ObserveRunHandle.__module__ == "openeight.telemetry"


def _client(response):
    return Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(lambda _: response),
    )


def _event(**changes):
    values = {
        "name": "refund.create",
        "observation_kind": "tool_call",
        "operation_kind": "invoke",
        "occurred_at": datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        "outcome": "success",
    }
    values.update(changes)
    return Event(**values)


def test_decide_returns_exact_frozen_correlation_result_and_discards_server_ids():
    response = httpx.Response(
        200,
        json={
            "decision_id": DECISION_ID,
            "verdict": "approval_required",
            "reason": "",
            "run_id": RUN_ID,
            "replayed": True,
            "agent_id": "server-agent-canary",
            "api_key_id": "server-key-canary",
            "policy_version_id": "server-policy-canary",
        },
    )
    client = _client(response)
    try:
        result = client.decide(
            decision_id=DECISION_ID,
            run_id=RUN_ID,
            tool_id="00000000-0000-4000-8000-000000000001",
            tool_version_id="00000000-0000-4000-8000-000000000002",
            source_time="2026-09-01T12:00:00Z",
            input={},
            policy_contract_set_digest="a" * 64,
            selected_values=[],
        )
    finally:
        client.close()

    assert [field.name for field in fields(result)] == [
        "decision_id",
        "verdict",
        "reason",
        "run_id",
        "replayed",
    ]
    assert result == DecisionResult(DECISION_ID, "approval_required", "", RUN_ID, True)
    assert result.needs_approval
    assert not result.allowed
    with pytest.raises(FrozenInstanceError):
        result.verdict = "allow"
    public_surface = repr(result) + repr(vars(result))
    assert "server-agent-canary" not in public_surface
    assert "server-key-canary" not in public_surface
    assert "server-policy-canary" not in public_surface


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {
            "decision_id": DECISION_ID,
            "verdict": "allow",
            "reason": "",
            "run_id": RUN_ID,
        },
        {
            "decision_id": DECISION_ID,
            "verdict": "unknown",
            "reason": "",
            "run_id": RUN_ID,
            "replayed": False,
        },
        {
            "decision_id": DECISION_ID,
            "verdict": "allow",
            "reason": 7,
            "run_id": RUN_ID,
            "replayed": False,
        },
        {
            "decision_id": DECISION_ID,
            "verdict": "allow",
            "reason": "",
            "run_id": "not-a-run",
            "replayed": False,
        },
        {
            "decision_id": DECISION_ID,
            "verdict": "allow",
            "reason": "",
            "run_id": RUN_ID,
            "replayed": 1,
        },
    ],
)
def test_decide_rejects_malformed_success_payload(payload):
    client = _client(httpx.Response(200, json=payload))
    try:
        with pytest.raises(ProtocolError) as caught:
            client.decide(
                decision_id=DECISION_ID,
                run_id=RUN_ID,
                tool_id="00000000-0000-4000-8000-000000000001",
                tool_version_id="00000000-0000-4000-8000-000000000002",
                source_time="2026-09-01T12:00:00Z",
                input={},
                policy_contract_set_digest="a" * 64,
                selected_values=[],
            )
    finally:
        client.close()
    assert caught.value.status_code == 200


def test_decide_malformed_success_reports_actual_2xx_status():
    client = _client(httpx.Response(201, json={}))
    try:
        with pytest.raises(ProtocolError) as caught:
            client.decide(
                decision_id=DECISION_ID,
                run_id=RUN_ID,
                tool_id="00000000-0000-4000-8000-000000000001",
                tool_version_id="00000000-0000-4000-8000-000000000002",
                source_time="2026-09-01T12:00:00Z",
                input={},
                policy_contract_set_digest="a" * 64,
                selected_values=[],
            )
    finally:
        client.close()
    assert caught.value.status_code == 201


@pytest.mark.parametrize("state", ["pending", "approved", "rejected", "expired"])
def test_approval_state_accepts_exact_server_vocabulary(state):
    client = _client(httpx.Response(200, json={"state": state}))
    try:
        assert client.approval_state(DECISION_ID) == state
    finally:
        client.close()


@pytest.mark.parametrize("decision_id", [None, "", "   ", "not-a-uuid"])
def test_approval_state_requires_explicit_uuid7_without_sending(decision_id):
    requests = []
    client = Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(500)
        ),
    )
    try:
        with pytest.raises(ConfigurationError) as caught:
            client.approval_state(decision_id)
    finally:
        client.close()
    assert caught.value.reason == "invalid_decision_id"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert requests == []


@pytest.mark.parametrize("state", ["", "timeout", "none", "APPROVED", 1, None])
def test_approval_state_rejects_unknown_server_vocabulary(state):
    client = _client(httpx.Response(200, json={"state": state}))
    try:
        with pytest.raises(ProtocolError):
            client.approval_state(DECISION_ID)
    finally:
        client.close()


def test_event_has_exact_id_free_immutable_surface_and_private_json_snapshots():
    source_input = {"amount": 7, "nested": ["kept"]}
    source_attributes = {"region": "test"}
    event = _event(
        decision_id=DECISION_ID,
        input=source_input,
        output={"accepted": True},
        attributes=source_attributes,
        latency_ms=0,
    )

    expected_parameters = [
        "name",
        "observation_kind",
        "operation_kind",
        "occurred_at",
        "outcome",
        "parent_event_id",
        "decision_id",
        "input",
        "output",
        "error",
        "attributes",
        "provider",
        "model",
        "tool_id",
        "tool_version_id",
        "tool_name",
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "latency_ms",
        "ttft_ms",
        "span_role",
    ]
    assert list(inspect.signature(Event).parameters) == expected_parameters
    assert not hasattr(event, "id")
    assert not hasattr(event, "event_id")
    assert not hasattr(event, "run_id")
    assert not hasattr(event, "agent_id")
    assert not hasattr(event, "workspace_id")
    assert not hasattr(event, "tool_version")
    assert event.latency_ms == 0
    assert event.ttft_ms is None

    source_input["nested"].append("caller-mutated")
    source_attributes["region"] = "caller-mutated"
    first = event.input
    first["nested"].append("accessor-mutated")
    assert event.input == {"amount": 7, "nested": ["kept"]}
    assert event.attributes == {"region": "test"}
    assert "amount" not in repr(event)
    assert "region" not in repr(event)
    with pytest.raises(FrozenInstanceError):
        event.name = "changed"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"name": ""}, "invalid_event"),
        ({"observation_kind": "other"}, "invalid_event"),
        ({"operation_kind": "other"}, "invalid_event"),
        ({"outcome": "other"}, "invalid_event"),
        ({"occurred_at": datetime(2026, 9, 1)}, "invalid_event"),
        ({"input": {"bad": float("nan")}}, "invalid_event"),
        ({"input": {1: "coerced-key"}}, "invalid_event"),
        ({"input": ("tuple",)}, "invalid_event"),
        ({"input": {"too_large": 2**63}}, "invalid_event"),
        ({"attributes": []}, "invalid_event"),
        ({"latency_ms": True}, "invalid_event_metrics"),
        ({"latency_ms": -1}, "invalid_event_metrics"),
        ({"latency_ms": 2**63}, "invalid_event_metrics"),
        ({"ttft_ms": 1}, "invalid_event_metrics"),
        ({"observation_kind": "llm_call", "ttft_ms": -1}, "invalid_event_metrics"),
        (
            {"observation_kind": "llm_call", "latency_ms": 2, "ttft_ms": 3},
            "invalid_event_metrics",
        ),
        ({"input_tokens": True}, "invalid_event_metrics"),
        ({"input_tokens": -1}, "invalid_event_metrics"),
        ({"input_tokens": 2**63}, "invalid_event_metrics"),
        ({"cache_creation_input_tokens": 1}, "invalid_event_metrics"),
        (
            {
                "input_tokens": 3,
                "cache_creation_input_tokens": 2,
                "cache_read_input_tokens": 2,
            },
            "invalid_event_metrics",
        ),
    ],
)
def test_event_rejects_invalid_shape_and_metric_relationships(changes, reason):
    with pytest.raises(ConfigurationError) as caught:
        _event(**changes)
    assert caught.value.reason == reason
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_event_preserves_null_and_zero_and_allows_llm_ttft_without_total_latency():
    absent = _event(observation_kind="llm_call")
    measured_zero = _event(observation_kind="llm_call", latency_ms=0, ttft_ms=0)
    ttft_only = _event(observation_kind="llm_call", ttft_ms=7)

    assert absent.latency_ms is None and absent.ttft_ms is None
    assert measured_zero.latency_ms == 0 and measured_zero.ttft_ms == 0
    assert ttft_only.latency_ms is None and ttft_only.ttft_ms == 7
