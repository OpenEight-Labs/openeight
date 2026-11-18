import inspect
import json
import logging

import httpx
import pytest

from openeight import APIError, Client, ConfigurationError, Event, TransportError

_DECISION_ID = "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b11"
_RUN_ID = "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b12"
_EVENT_ID = "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b13"


def _terminal_event(name="langchain.agent"):
    return Event(
        name=name,
        observation_kind="span",
        operation_kind="invoke",
        occurred_at="2026-09-01T12:00:01Z",
        outcome="error",
        error={"kind": "agent_error"},
    )


def test_terminal_replay_has_exact_keyword_only_shape_and_reuses_wire_bytes(
    monkeypatch,
):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            raise httpx.ReadError("lost acknowledgement", request=request)
        body = requests[-1]
        return httpx.Response(
            200,
            json={
                field: [{"id": write["id"], "replayed": True} for write in body[field]]
                for field in ("runs", "events")
            },
        )

    client = Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="worker",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(
        "openeight.telemetry.uuid7",
        lambda: pytest.fail("terminal replay generated a new identifier"),
    )
    arguments = {
        "run_id": _RUN_ID,
        "started_at": "2026-09-01T12:00:00Z",
        "ended_at": "2026-09-01T12:00:03Z",
        "event_id": _EVENT_ID,
        "event": _terminal_event(),
    }

    parameters = list(
        inspect.signature(Client._write_terminal_replay).parameters.values()
    )
    assert [parameter.name for parameter in parameters] == ["self", *arguments]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY for parameter in parameters[1:]
    )

    with pytest.raises(TransportError):
        client._write_terminal_replay(**arguments)
    client._write_terminal_replay(**arguments)

    assert (
        requests[0]
        == requests[1]
        == {
            "runs": [
                {
                    "id": _RUN_ID,
                    "status": "completed",
                    "started_at": "2026-09-01T12:00:00Z",
                    "ended_at": "2026-09-01T12:00:03Z",
                    "attributes_json": "{}",
                }
            ],
            "events": [
                {
                    **_terminal_event()._as_write(),
                    "id": _EVENT_ID,
                    "run_id": _RUN_ID,
                }
            ],
        }
    )
    client.close()


def test_terminal_replay_request_preserves_distinct_event_and_run_times():
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                field: [{"id": write["id"], "replayed": False} for write in body[field]]
                for field in ("runs", "events")
            },
        )

    client = Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="worker",
        transport=httpx.MockTransport(handler),
    )
    arguments = {
        "run_id": _RUN_ID,
        "started_at": "2026-09-01T12:00:00Z",
        "ended_at": "2026-09-01T12:00:03Z",
        "event_id": _EVENT_ID,
        "event": _terminal_event(),
    }
    client._write_terminal_replay(**arguments)

    assert len(requests) == 1
    assert requests[0]["runs"][0]["started_at"] == "2026-09-01T12:00:00Z"
    assert requests[0]["events"][0]["occurred_at"] == "2026-09-01T12:00:01Z"
    assert requests[0]["runs"][0]["ended_at"] == "2026-09-01T12:00:03Z"
    assert requests[0]["events"][0]["occurred_at"] != requests[0]["runs"][0]["ended_at"]
    client.close()


def test_terminal_replay_surfaces_conflict_when_any_wire_byte_changes():
    committed = None
    requests = []

    def handler(request):
        nonlocal committed
        requests.append(request.content)
        body = json.loads(request.content)
        if committed is None:
            committed = request.content
        elif request.content != committed:
            return httpx.Response(
                409,
                json={"code": "already_exists", "message": "conflict"},
            )
        return httpx.Response(
            200,
            json={
                field: [
                    {"id": write["id"], "replayed": committed == request.content}
                    for write in body[field]
                ]
                for field in ("runs", "events")
            },
        )

    client = Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="worker",
        transport=httpx.MockTransport(handler),
    )
    arguments = {
        "run_id": _RUN_ID,
        "started_at": "2026-09-01T12:00:00Z",
        "ended_at": "2026-09-01T12:00:03Z",
        "event_id": _EVENT_ID,
        "event": _terminal_event(),
    }
    client._write_terminal_replay(**arguments)
    client._write_terminal_replay(**arguments)

    changed = dict(arguments)
    changed["event"] = _terminal_event("langchain.agent.changed")
    with pytest.raises(APIError) as caught:
        client._write_terminal_replay(**changed)

    assert caught.value.category == "already_exists"
    assert caught.value.code == "already_exists"
    assert requests[0] == requests[1] == committed
    assert requests[2] != committed
    client.close()


@pytest.mark.parametrize("value", [1 << 63, "x" * 65535], ids=["integer", "token"])
def test_decide_rejects_runtime_json_before_transport(value):
    requests = []
    with Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="worker",
        transport=httpx.MockTransport(lambda request: requests.append(request)),
    ) as client:
        with pytest.raises(ConfigurationError) as error:
            client.decide(
                tool_id="00000000-0000-4000-8000-000000000001",
                tool_version_id="00000000-0000-4000-8000-000000000002",
                input={},
                selected_values=[{"path": "value", "value": value}],
                policy_contract_set_digest="a" * 64,
            )
    assert error.value.reason == "invalid_input"
    assert error.value.__cause__ is None
    assert requests == []


@pytest.mark.parametrize(
    "code", ["TOOL_NOT_GOVERNED", "EXECUTION_BLOCKED", "AUTHORIZATION_STALE"]
)
def test_decide_preserves_precondition_error_codes(code):
    from openeight import APIError

    with Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="worker",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                409, json={"error": "conflict", "code": code, "message": "safe"}
            )
        ),
    ) as client:
        with pytest.raises(APIError) as error:
            client.decide(
                tool_id="00000000-0000-4000-8000-000000000001",
                tool_version_id="00000000-0000-4000-8000-000000000002",
                input={},
                selected_values=[{"path": "country", "value": "US"}],
                policy_contract_set_digest="a" * 64,
            )
    assert error.value.code == code


def test_authenticated_routes_use_only_machine_identity_headers_and_health_is_anonymous():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/v1/openeight.govern.v1.GovernService/Decide":
            return httpx.Response(
                200,
                json={
                    "decision_id": _DECISION_ID,
                    "verdict": "allow",
                    "reason": "",
                    "run_id": _RUN_ID,
                    "replayed": False,
                    "agent_id": "server-agent-id-must-be-ignored",
                    "api_key_id": "server-key-id-must-be-ignored",
                },
            )
        if request.url.path.endswith("/GetApprovalState"):
            return httpx.Response(200, json={"state": "approved"})
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                field: [{"id": write["id"], "replayed": False} for write in body[field]]
                for field in ("runs", "events")
            },
        )

    with Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(handler),
    ) as client:
        client.health()
        result = client.decide(
            decision_id=_DECISION_ID,
            run_id=_RUN_ID,
            tool_id="00000000-0000-4000-8000-000000000001",
            tool_version_id="00000000-0000-4000-8000-000000000002",
            input={},
            source_time="2026-08-31T12:00:00Z",
            selected_values=[{"path": "amount", "value": 7}],
            policy_contract_set_digest="a" * 64,
        )
        client.approval_state(_DECISION_ID)
        client.write_telemetry(runs=[], events=[])
        writer = client.buffered_observe()
        writer.begin_run(_RUN_ID).submit({"name": "buffered"}, completed=True)
        writer.close()

    health, decide, approval, observe, buffered_observe = requests
    assert health.url.path == "/healthz"

    def authority_and_identity_headers(request):
        return {
            name.lower(): value
            for name, value in request.headers.items()
            if name.lower() == "authorization" or name.lower().startswith("x-")
        }

    assert authority_and_identity_headers(health) == {}

    expected_machine_headers = {
        "authorization": "ApiKey ctk_secret",
        "x-openeight-agent-name": "runtime/worker",
    }
    for request in (decide, approval, observe, buffered_observe):
        assert authority_and_identity_headers(request) == expected_machine_headers

    assert decide.method == approval.method == "POST"
    assert json.loads(approval.content) == {"decision_id": _DECISION_ID}
    body = json.loads(decide.content)
    assert body["input_json"] == "{}"
    assert json.loads(body["selected_values_json"]) == [{"path": "amount", "value": 7}]
    assert "input" not in body and "selected_values" not in body
    assert "agent_id" not in body
    assert "api_key_id" not in body
    assert "workspace_id" not in body
    assert "credential_id" not in body
    assert "policy_id" not in body
    assert "policy_version_id" not in body
    result_surface = repr(result) + repr(vars(result))
    assert "server-agent-id-must-be-ignored" not in result_surface
    assert "server-key-id-must-be-ignored" not in result_surface


def test_approval_state_applies_a_per_request_timeout_override():
    request_timeouts = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, json={"state": "approved"})

    with Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="runtime/worker",
        timeout=30.0,
        transport=httpx.MockTransport(handler),
    ) as client:
        assert client.approval_state(_DECISION_ID, timeout=1.0) == "approved"

    assert len(request_timeouts) == 1
    assert all(0 < value <= 1.0 for value in request_timeouts[0].values())


@pytest.mark.parametrize(
    "operation",
    [
        lambda client: client.health(),
        lambda client: client.decide(
            tool_id="00000000-0000-4000-8000-000000000001",
            tool_version_id="00000000-0000-4000-8000-000000000002",
            input={},
            policy_contract_set_digest="a" * 64,
            selected_values=[],
        ),
        lambda client: client.approval_state(_DECISION_ID),
        lambda client: client.write_telemetry(runs=[], events=[]),
        lambda client: client.buffered_observe(),
        lambda client: client.__enter__(),
    ],
)
def test_every_operation_and_resource_factory_rejects_use_after_close(operation):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"status": "ok"})

    client = Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(handler),
    )
    client.close()

    with pytest.raises(ConfigurationError) as caught:
        operation(client)
    assert caught.value.reason == "client_closed"
    assert caught.value.message == "SDK client is closed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert calls == []


class _CloseFailureTransport(httpx.BaseTransport):
    def __init__(self, canary: str) -> None:
        self.canary = canary
        self.close_calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    def close(self) -> None:
        self.close_calls += 1
        raise RuntimeError(self.canary)


def test_close_failure_is_safe_and_replayed_without_reclosing(caplog):
    canary = "close-failure-secret-canary"
    transport = _CloseFailureTransport(canary)
    client = Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="runtime/worker",
        transport=transport,
    )
    caplog.set_level(logging.DEBUG)

    for _ in range(2):
        with pytest.raises(TransportError) as caught:
            client.close()
        error = caught.value
        assert error.__cause__ is None
        assert error.__context__ is None
        assert canary not in str(error) + repr(error) + repr(vars(error)) + caplog.text

    assert transport.close_calls == 1


def test_context_exit_without_body_failure_reports_safe_close_failure():
    transport = _CloseFailureTransport("context-close-secret-canary")

    with pytest.raises(TransportError) as caught:
        with Client(
            "https://control.example",
            api_key="ctk_secret",
            agent_name="runtime/worker",
            transport=transport,
        ):
            pass

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "context-close-secret-canary" not in repr(caught.value)


def test_context_body_failure_takes_precedence_over_close_failure():
    class BodyFailure(Exception):
        pass

    body_error = BodyFailure("body-failure-canary")
    transport = _CloseFailureTransport("masked-close-secret-canary")

    with pytest.raises(BodyFailure) as caught:
        with Client(
            "https://control.example",
            api_key="ctk_secret",
            agent_name="runtime/worker",
            transport=transport,
        ):
            raise body_error

    assert caught.value is body_error
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert transport.close_calls == 1


def test_client_keeps_machine_secret_out_of_public_attributes():
    client = Client(
        "https://control.example",
        api_key="ctk_secret_canary",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"status": "ok"})
        ),
    )
    try:
        public = {
            name: value
            for name, value in vars(client).items()
            if not name.startswith("_")
        }
        assert "ctk_secret_canary" not in repr(public)
        assert "ctk_secret_canary" not in repr(client)
    finally:
        client.close()


@pytest.mark.parametrize("phase", ["headers", "body"])
@pytest.mark.parametrize("bound", ["adapter_deadline", "request_timeout"])
def test_adapter_owned_request_has_total_deadline(monkeypatch, phase, bound):
    import asyncio
    import time

    from openeight.client import _ADAPTER_DEADLINE

    settled = []

    class Drip(httpx.SyncByteStream, httpx.AsyncByteStream):
        def __iter__(self):
            try:
                for byte in b'{"status":"ok"}':
                    time.sleep(0.02)
                    yield bytes([byte])
            finally:
                settled.append(True)

        async def __aiter__(self):
            try:
                for byte in b'{"status":"ok"}':
                    await asyncio.sleep(0.02)
                    yield bytes([byte])
            finally:
                settled.append(True)

    def handle(request):
        if phase == "headers":
            time.sleep(0.3)
            settled.append(True)
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, stream=Drip())

    async def ahandle(request):
        if phase == "headers":
            try:
                await asyncio.sleep(0.3)
                return httpx.Response(200, json={"status": "ok"})
            finally:
                settled.append(True)
        return httpx.Response(200, stream=Drip())

    transport = httpx.MockTransport(handle)
    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: async_client(transport=httpx.MockTransport(ahandle), **kwargs),
    )
    client = Client(
        "http://localhost", api_key="ctk_test", agent_name="test", transport=transport
    )
    client._adapter_owned_send = True
    started = time.monotonic()
    token = _ADAPTER_DEADLINE.set(
        started + 0.05 if bound == "adapter_deadline" else None
    )
    try:
        with pytest.raises(TransportError):
            if bound == "adapter_deadline":
                client.health()
            else:
                client._send(
                    "GET",
                    "/healthz",
                    authenticated=False,
                    protocol="rest",
                    timeout=0.05,
                )
        assert time.monotonic() - started < 0.15
        assert settled == [True]
    finally:
        _ADAPTER_DEADLINE.reset(token)
        network = client._adapter_http
        client.close()
        if network is not None:
            assert not network.thread.is_alive()


def test_connect_decision_replay_retains_wire_bytes_and_integer_precision():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == "/api/v1/openeight.govern.v1.GovernService/Decide"
        if len(requests) == 1:
            raise httpx.ReadError("lost acknowledgement", request=request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "decision_id": body["decision_id"],
                "run_id": body["run_id"],
                "verdict": "approval_required",
                "reason": "",
                "replayed": True,
            },
        )

    args = dict(
        decision_id=_DECISION_ID,
        run_id=_RUN_ID,
        tool_id="00000000-0000-4000-8000-000000000001",
        tool_version_id="00000000-0000-4000-8000-000000000002",
        input={},
        source_time="2026-08-31T12:00:00.123456Z",
        selected_values=[{"path": "amount", "value": 9223372036854775807}],
        policy_contract_set_digest="a" * 64,
    )
    with Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="worker",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(TransportError):
            client.decide(**args)
        assert len(requests) == 1
        result = client.decide(**args)
    assert result.verdict == "approval_required" and result.replayed
    assert requests[0].content == requests[1].content
    body = json.loads(requests[0].content)
    assert body["source_time"] == args["source_time"]
    assert json.loads(body["selected_values_json"]) == args["selected_values"]


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_decide_rejects_invalid_timeout_before_transport(timeout):
    def handler(request):
        pytest.fail("invalid timeout reached transport")

    with Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="worker",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ConfigurationError):
            client.decide(
                tool_id=_DECISION_ID,
                tool_version_id=_EVENT_ID,
                input={},
                policy_contract_set_digest="a" * 64,
                selected_values=[],
                timeout=timeout,
            )


def test_decide_timeout_changes_transport_budget_without_changing_replay_bytes():
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "decision_id": body["decision_id"],
                "run_id": body["run_id"],
                "verdict": "approval_required",
                "reason": "",
                "replayed": len(requests) > 1,
            },
        )

    args = dict(
        decision_id=_DECISION_ID,
        run_id=_RUN_ID,
        source_time="2026-08-31T12:00:00Z",
        tool_id=_EVENT_ID,
        tool_version_id="00000000-0000-4000-8000-000000000002",
        input={},
        policy_contract_set_digest="a" * 64,
        selected_values=[],
    )
    with Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="worker",
        timeout=30.0,
        transport=httpx.MockTransport(handler),
    ) as client:
        client.decide(**args)
        assert client.decide(**args, timeout=0.5).replayed
    assert requests[0].content == requests[1].content
    assert all(0 < value <= 0.5 for value in requests[1].extensions["timeout"].values())
    assert requests[0].extensions["timeout"]["read"] == 30.0


def test_decide_timeout_stops_waiting_for_a_slow_custom_transport():
    import threading

    release = threading.Event()
    settled = threading.Event()

    def handler(request):
        try:
            release.wait(2)
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "decision_id": body["decision_id"],
                    "run_id": body["run_id"],
                    "verdict": "allow",
                    "reason": "",
                    "replayed": False,
                },
            )
        finally:
            settled.set()

    with Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="worker",
        transport=httpx.MockTransport(handler),
    ) as client:
        try:
            with pytest.raises(TransportError):
                client.decide(
                    tool_id=_DECISION_ID,
                    tool_version_id=_EVENT_ID,
                    input={},
                    policy_contract_set_digest="a" * 64,
                    selected_values=[],
                    timeout=0.01,
                )
        finally:
            release.set()
            assert settled.wait(2)
