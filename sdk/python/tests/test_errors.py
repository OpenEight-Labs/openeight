import logging
from collections.abc import Mapping
from types import FunctionType, MethodType

import httpx
import pytest

from openeight import (
    APIError,
    ApprovalTimeout,
    BlockedError,
    Client,
    ConfigurationError,
    ObserveDeliveryError,
    ObserveQueueFullError,
    ObserveRunCompletedError,
    ProtocolError,
    SDKError,
    TransportError,
)


def _unsafe_value_reachable(value, canaries, seen=None, depth=0):
    if seen is None:
        seen = set()
    if depth > 12 or id(value) in seen:
        return False
    seen.add(id(value))
    if isinstance(value, str):
        return any(canary in value for canary in canaries)
    if isinstance(value, bytes):
        return any(canary.encode() in value for canary in canaries)
    if isinstance(value, Mapping):
        return any(
            _unsafe_value_reachable(item, canaries, seen, depth + 1)
            for pair in value.items()
            for item in pair
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(
            _unsafe_value_reachable(item, canaries, seen, depth + 1) for item in value
        )
    if isinstance(value, httpx.Request):
        return _unsafe_value_reachable(
            (str(value.url), dict(value.headers), value.content),
            canaries,
            seen,
            depth + 1,
        )
    if isinstance(value, httpx.Response):
        return _unsafe_value_reachable(
            (dict(value.headers), value.content, value.request),
            canaries,
            seen,
            depth + 1,
        )
    if isinstance(value, BaseException):
        return any(
            _unsafe_value_reachable(item, canaries, seen, depth + 1)
            for item in (vars(value), value.__cause__, value.__context__)
        )
    if isinstance(value, FunctionType):
        if _unsafe_value_reachable(
            value.__defaults__, canaries, seen, depth + 1
        ) or _unsafe_value_reachable(value.__kwdefaults__, canaries, seen, depth + 1):
            return True
        for cell in value.__closure__ or ():
            try:
                if _unsafe_value_reachable(
                    cell.cell_contents, canaries, seen, depth + 1
                ):
                    return True
            except ValueError:
                pass
        return False
    if isinstance(value, MethodType):
        return any(
            _unsafe_value_reachable(item, canaries, seen, depth + 1)
            for item in (value.__self__, value.__func__)
        )
    if type(value).__module__.startswith("openeight") and hasattr(value, "__dict__"):
        return _unsafe_value_reachable(vars(value), canaries, seen, depth + 1)
    return False


def _assert_exported_error_has_no_unsafe_sdk_traceback_state(error, canaries):
    assert error.__cause__ is None
    assert error.__context__ is None
    assert not _unsafe_value_reachable(vars(error), canaries)
    assert not _unsafe_value_reachable(str(error), canaries)
    assert not _unsafe_value_reachable(repr(error), canaries)
    traceback = error.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        module = frame.f_globals.get("__name__", "")
        if module == "openeight" or module.startswith("openeight."):
            assert not _unsafe_value_reachable(frame.f_locals, canaries), (
                frame.f_code.co_name,
                sorted(frame.f_locals),
            )
        traceback = traceback.tb_next


@pytest.mark.parametrize(
    "reason",
    [
        "invalid_tool_definition",
        "duplicate_tool_definition",
        "invalid_tool_description",
        "invalid_tool_schema",
        "tool_schema_too_large",
        "invalid_poll_interval",
        "invalid_poll_timeout",
        "invalid_fields",
        "invalid_tool",
        "invalid_governed_tool_spec",
        "invalid_otlp_endpoint",
        "otlp_already_configured",
        "otlp_closing",
    ],
)
def test_unshipped_configuration_vocabulary_is_not_public(reason):
    error = ConfigurationError(reason)
    assert error.reason == "invalid_configuration"
    assert error.message == "invalid configuration"


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        (
            "invalid_integration_configuration",
            "LangChain integration configuration is invalid",
        ),
        (
            "integration_not_entered",
            "LangChain instrumentation is not entered",
        ),
        ("integration_closed", "LangChain instrumentation is closed"),
        ("invalid_resume_correlation", "durable resume correlation is invalid"),
        ("execution_busy", "LangChain execution is busy"),
    ],
)
def test_langchain_configuration_reasons_have_fixed_safe_messages(reason, message):
    error = ConfigurationError(reason)
    assert (error.reason, error.message, str(error)) == (reason, message, message)


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        (
            "suspend_flush_failed",
            "Governed Run suspension flush failed",
        ),
        (
            "suspend_fresh_delivery_required",
            "Governed Run requires fresh delivery",
        ),
    ],
)
def test_suspension_delivery_reasons_have_fixed_safe_messages(reason, message):
    error = ObserveDeliveryError(reason)
    assert (error.reason, error.message, str(error)) == (reason, message, message)


REST_ERRORS = [
    (400, "bad_request", "AMBIGUOUS_WORKSPACE"),
    (503, "service_unavailable", "AUTH_NOT_CONFIGURED"),
    (503, "service_unavailable", "AUTH_VALIDATION_UNAVAILABLE"),
    (502, "bad_gateway", "BAD_GATEWAY"),
    (400, "bad_request", "BAD_REQUEST"),
    (409, "conflict", "CONFLICT"),
    (403, "forbidden", "FORBIDDEN"),
    (500, "internal_error", "INTERNAL_ERROR"),
    (404, "not_found", "NOT_FOUND"),
    (413, "payload_too_large", "PAYLOAD_TOO_LARGE"),
    (422, "unprocessable_entity", "POLICY_INVALID"),
    (429, "rate_limited", "RATE_LIMITED"),
    (503, "service_unavailable", "SERVICE_UNAVAILABLE"),
    (401, "unauthorized", "UNAUTHENTICATED"),
    (415, "unsupported_media_type", "UNSUPPORTED_MEDIA_TYPE"),
    (422, "unprocessable_entity", "UNPROCESSABLE_ENTITY"),
    (403, "forbidden", "USER_NOT_ACTIVE"),
    (403, "forbidden", "WORKSPACE_FORBIDDEN"),
]

CONNECT_CODES = [
    "canceled",
    "unknown",
    "invalid_argument",
    "deadline_exceeded",
    "not_found",
    "already_exists",
    "permission_denied",
    "resource_exhausted",
    "failed_precondition",
    "aborted",
    "out_of_range",
    "unimplemented",
    "internal",
    "unavailable",
    "data_loss",
    "unauthenticated",
]


@pytest.mark.parametrize(("status", "category", "code"), REST_ERRORS)
def test_every_published_rest_error_maps_to_safe_api_error(status, category, code):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"X-Canary": "header-secret"},
            json={
                "error": category,
                "code": code,
                "message": "server-message-secret",
                "agent_id": "server-agent-secret",
            },
        )

    client = Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(APIError) as caught:
            client.health()
    finally:
        client.close()

    error = caught.value
    assert error.status_code == status
    assert error.category == category
    assert error.code == code
    assert error.message == "request failed"
    assert str(error) == "request failed"
    public = repr(error) + repr(vars(error))
    for canary in ("header-secret", "server-message-secret", "server-agent-secret"):
        assert canary not in public
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize("code", CONNECT_CODES)
def test_every_canonical_connect_code_maps_without_rest_rewriting(code):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"code": code, "message": "connect-message-secret"},
        )

    client = Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(APIError) as caught:
            client.write_telemetry(runs=[], events=[])
    finally:
        client.close()

    assert caught.value.status_code == 400
    assert caught.value.category == code
    assert caught.value.code == code
    assert caught.value.message == "request failed"
    assert "connect-message-secret" not in repr(caught.value)
    assert "connect-message-secret" not in repr(vars(caught.value))


def test_connect_route_accepts_safe_rest_authentication_error_envelope():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": "unauthorized",
                "code": "UNAUTHENTICATED",
                "message": "authentication-detail-secret",
            },
        )

    client = Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(APIError) as caught:
            client.write_telemetry(runs=[], events=[])
    finally:
        client.close()

    assert caught.value.status_code == 401
    assert caught.value.category == "unauthorized"
    assert caught.value.code == "UNAUTHENTICATED"
    assert "authentication-detail-secret" not in repr(caught.value)


def test_newer_rest_reason_is_not_retained_or_replaced_with_sdk_code():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": "bad_request",
                "code": "NEW_SERVER_REASON_CANARY",
                "message": "server-message-canary",
            },
        )

    client = Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(APIError) as caught:
            client.health()
    finally:
        client.close()

    assert caught.value.category == "bad_request"
    assert caught.value.code is None
    public = str(caught.value) + repr(caught.value) + repr(vars(caught.value))
    assert "NEW_SERVER_REASON_CANARY" not in public
    assert "server-message-canary" not in public


@pytest.mark.parametrize(
    ("response", "operation"),
    [
        (httpx.Response(502, content=b"body-secret-not-json"), "rest"),
        (httpx.Response(400, json=["body-secret-list"]), "rest"),
        (
            httpx.Response(
                400, json={"error": "bad_request", "code": 7, "message": "x"}
            ),
            "rest",
        ),
        (
            httpx.Response(
                400,
                json={
                    "error": ["rest-list-canary"],
                    "code": "BAD_REQUEST",
                    "message": "x",
                },
            ),
            "rest",
        ),
        (
            httpx.Response(
                400,
                json={
                    "error": {"rest-dict-canary": True},
                    "code": "BAD_REQUEST",
                    "message": "x",
                },
            ),
            "rest",
        ),
        (
            httpx.Response(
                400,
                json={
                    "error": "unknown-category-canary",
                    "code": "BAD",
                    "message": "x",
                },
            ),
            "rest",
        ),
        (
            httpx.Response(
                400,
                json={"code": "not_a_connect_code", "message": "body-secret"},
            ),
            "connect",
        ),
        (
            httpx.Response(400, json={"code": ["connect-list-canary"], "message": "x"}),
            "connect",
        ),
        (
            httpx.Response(
                400, json={"code": {"connect-dict-canary": True}, "message": "x"}
            ),
            "connect",
        ),
        (httpx.Response(302, json={"status": "redirect-body-secret"}), "rest"),
        (httpx.Response(200, content=b"success-body-secret-not-json"), "rest"),
        (httpx.Response(200, json=["success-body-secret-list"]), "rest"),
    ],
)
def test_malformed_error_and_success_responses_fail_closed_without_disclosure(
    response, operation
):
    client = Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(lambda _: response),
    )
    try:
        with pytest.raises(ProtocolError) as caught:
            if operation == "connect":
                client.write_telemetry(runs=[], events=[])
            else:
                client.health()
    finally:
        client.close()

    error = caught.value
    assert error.status_code == response.status_code
    assert error.message == "invalid server response"
    public = str(error) + repr(error) + repr(vars(error))
    for canary in (
        "body-secret",
        "success-body-secret",
        "unknown-category-canary",
        "rest-list-canary",
        "rest-dict-canary",
        "connect-list-canary",
        "connect-dict-canary",
    ):
        assert canary not in public
    assert error.__cause__ is None
    assert error.__context__ is None


def test_transport_exception_is_reduced_after_except_scope_and_never_logged(caplog):
    request_canary = "https://transport-request-secret.example"
    reason_canary = "transport-reason-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            reason_canary, request=httpx.Request("GET", request_canary)
        )

    client = Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(handler),
    )
    caplog.set_level(logging.DEBUG)
    try:
        with pytest.raises(TransportError) as caught:
            client.health()
    finally:
        client.close()

    error = caught.value
    assert error.message == "transport request failed"
    assert str(error) == "transport request failed"
    assert error.__cause__ is None
    assert error.__context__ is None
    public = str(error) + repr(error) + repr(vars(error)) + caplog.text
    assert request_canary not in public
    assert reason_canary not in public


@pytest.mark.parametrize("response_kind", ["malformed-success", "malformed-error"])
def test_exported_protocol_errors_retain_no_raw_sdk_traceback_state(response_kind):
    canaries = {
        "ctk_traceback_api_key_canary",
        "traceback-request-header-canary",
        "traceback-raw-body-canary",
    }

    def handler(request):
        status = 200 if response_kind == "malformed-success" else 502
        return httpx.Response(
            status,
            headers={"X-Traceback-Canary": "traceback-request-header-canary"},
            content=b"traceback-raw-body-canary",
            request=request,
        )

    client = Client(
        "https://control.example",
        api_key="ctk_traceback_api_key_canary",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ProtocolError) as caught:
            client.write_telemetry(runs=[], events=[])
        _assert_exported_error_has_no_unsafe_sdk_traceback_state(
            caught.value,
            canaries,
        )
    finally:
        client.close()


def test_exported_transport_error_retains_no_request_or_httpx_traceback_state():
    canaries = {
        "ctk_traceback_transport_key",
        "traceback-transport-url-canary",
        "traceback-transport-header-canary",
    }

    def handler(request):
        raw_request = httpx.Request(
            "POST",
            "https://traceback-transport-url-canary.example",
            headers={"X-Canary": "traceback-transport-header-canary"},
        )
        raise httpx.ConnectError("transport failed", request=raw_request)

    client = Client(
        "https://control.example",
        api_key="ctk_traceback_transport_key",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(TransportError) as caught:
            client.write_telemetry(runs=[], events=[])
        _assert_exported_error_has_no_unsafe_sdk_traceback_state(
            caught.value,
            canaries,
        )
    finally:
        client.close()


@pytest.mark.parametrize("operation", ["health", "decide", "approval"])
def test_each_request_api_sanitizes_exported_transport_traceback(operation):
    canaries = {
        "ctk_traceback_request_api_key",
        "traceback-request-api-url-canary",
    }

    def handler(_request):
        raise httpx.ConnectError(
            "failed",
            request=httpx.Request(
                "GET",
                "https://traceback-request-api-url-canary.example",
            ),
        )

    client = Client(
        "https://control.example",
        api_key="ctk_traceback_request_api_key",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(TransportError) as caught:
            if operation == "health":
                client.health()
            elif operation == "decide":
                client.decide(
                    tool_id="00000000-0000-4000-8000-000000000001",
                    tool_version_id="00000000-0000-4000-8000-000000000002",
                    input={},
                    policy_contract_set_digest="a" * 64,
                    selected_values=[],
                )
            else:
                client.approval_state("018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b11")
        _assert_exported_error_has_no_unsafe_sdk_traceback_state(caught.value, canaries)
    finally:
        client.close()


def test_exported_close_failure_retains_no_resource_or_identity_traceback_state():
    canaries = {
        "ctk_traceback_close_key",
        "traceback-close-resource-canary",
    }

    class FailingCloseTransport(httpx.BaseTransport):
        def handle_request(self, request):
            return httpx.Response(200, json={"status": "ok"}, request=request)

        def close(self):
            raise RuntimeError("traceback-close-resource-canary")

    client = Client(
        "https://control.example",
        api_key="ctk_traceback_close_key",
        agent_name="runtime/worker",
        transport=FailingCloseTransport(),
    )
    with pytest.raises(TransportError) as caught:
        client.close()

    _assert_exported_error_has_no_unsafe_sdk_traceback_state(caught.value, canaries)


def test_exported_writer_close_failure_retains_no_raw_event_traceback_state():
    canaries = {"traceback-writer-event-canary", "traceback-writer-sender-canary"}

    def sender(*_):
        raise RuntimeError("traceback-writer-sender-canary")

    from openeight import BufferedObserveWriter

    writer = BufferedObserveWriter(sender, flush_records=100, flush_interval=60)
    writer.begin_run().submit(
        {"name": "traceback-writer-event-canary"},
    )
    with pytest.raises(ObserveDeliveryError) as caught:
        writer.close()

    _assert_exported_error_has_no_unsafe_sdk_traceback_state(caught.value, canaries)


def test_exported_governed_run_close_failure_retains_no_client_or_event_state():
    canaries = {
        "ctk_traceback_run_close_key",
        "traceback-run-event-canary",
        "traceback-run-server-canary",
    }

    def handler(request):
        import json

        body = json.loads(request.content)
        if body["events"]:
            return httpx.Response(
                503,
                json={"code": "unavailable", "message": "traceback-run-server-canary"},
                request=request,
            )
        return httpx.Response(
            200,
            json={
                field: [{"id": write["id"], "replayed": False} for write in body[field]]
                for field in ("runs", "events")
            },
            request=request,
        )

    from openeight import Event

    client = Client(
        "https://control.example",
        api_key="ctk_traceback_run_close_key",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(handler),
    )
    run = client.governed_run()
    run.submit_event(
        Event(
            name="traceback-run-event-canary",
            tool_name="traceback-run-event-canary",
            observation_kind="tool_call",
            operation_kind="invoke",
            occurred_at="2026-09-01T12:00:00Z",
            outcome="success",
        )
    )
    with pytest.raises(ObserveDeliveryError) as caught:
        run.close()

    _assert_exported_error_has_no_unsafe_sdk_traceback_state(caught.value, canaries)
    try:
        client.close()
    except ObserveDeliveryError:
        pass


@pytest.mark.parametrize(
    ("failure_stage", "expected_reason"),
    [
        ("flush", "suspend_flush_failed"),
        ("writer_close", "suspend_fresh_delivery_required"),
    ],
)
def test_exported_suspend_failure_retains_no_client_event_or_cause_state(
    monkeypatch,
    failure_stage,
    expected_reason,
):
    canaries = {
        "ctk_traceback_suspend_key",
        "traceback-suspend-event-canary",
        "traceback-suspend-server-canary",
        "traceback-suspend-close-canary",
    }
    fail_flush = failure_stage == "flush"

    def handler(request):
        import json

        body = json.loads(request.content)
        if body["events"] and fail_flush:
            raise RuntimeError("traceback-suspend-server-canary")
        return httpx.Response(
            200,
            json={
                field: [{"id": write["id"], "replayed": False} for write in body[field]]
                for field in ("runs", "events")
            },
            request=request,
        )

    from openeight import Event

    client = Client(
        "https://control.example",
        api_key="ctk_traceback_suspend_key",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(handler),
    )
    run = client.governed_run()
    run.submit_event(
        Event(
            name="traceback-suspend-event-canary",
            tool_name="traceback-suspend-event-canary",
            observation_kind="tool_call",
            operation_kind="invoke",
            occurred_at="2026-09-01T12:00:00Z",
            outcome="success",
        )
    )
    if failure_stage == "writer_close":
        monkeypatch.setattr(
            run._writer,
            "close",
            lambda *, timeout: (_ for _ in ()).throw(
                RuntimeError("traceback-suspend-close-canary")
            ),
        )

    with pytest.raises(ObserveDeliveryError) as caught:
        run.suspend()

    assert caught.value.reason == expected_reason
    _assert_exported_error_has_no_unsafe_sdk_traceback_state(caught.value, canaries)
    if failure_stage == "flush":
        fail_flush = False
        run.suspend()
    client.close()


def test_exported_terminal_replay_failure_retains_no_client_or_event_state():
    canaries = {
        "ctk_traceback_terminal_replay_key",
        "traceback-terminal-replay-event-canary",
        "traceback-terminal-replay-server-canary",
    }

    def handler(_request):
        raise RuntimeError("traceback-terminal-replay-server-canary")

    from openeight import Event

    client = Client(
        "https://control.example",
        api_key="ctk_traceback_terminal_replay_key",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(handler),
    )
    event = Event(
        name="traceback-terminal-replay-event-canary",
        observation_kind="span",
        operation_kind="invoke",
        occurred_at="2026-09-01T12:00:01Z",
        outcome="error",
        error={"kind": "agent_error"},
    )

    with pytest.raises(TransportError) as caught:
        client._write_terminal_replay(
            run_id="018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b12",
            started_at="2026-09-01T12:00:00Z",
            ended_at="2026-09-01T12:00:03Z",
            event_id="018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b13",
            event=event,
        )

    _assert_exported_error_has_no_unsafe_sdk_traceback_state(caught.value, canaries)
    client.close()


@pytest.mark.parametrize("trigger", ["explicit", "threshold"])
def test_buffered_flush_errors_drop_raw_events_from_sdk_tracebacks(trigger):
    event_canary = f"writer-{trigger}-event-canary"
    sender_canary = f"writer-{trigger}-sender-canary"

    def sender(*_):
        raise RuntimeError(sender_canary)

    from openeight import BufferedObserveWriter

    writer = BufferedObserveWriter(
        sender,
        flush_records=1 if trigger == "threshold" else 100,
        flush_interval=60,
    )
    run = writer.begin_run()
    try:
        if trigger == "explicit":
            run.submit({"name": event_canary})
            with pytest.raises(ObserveDeliveryError) as caught:
                writer.flush()
        else:
            with pytest.raises(ObserveDeliveryError) as caught:
                run.submit({"name": event_canary})
        _assert_exported_error_has_no_unsafe_sdk_traceback_state(
            caught.value,
            {event_canary, sender_canary},
        )
    finally:
        try:
            writer.close(timeout=0.01)
        except ObserveDeliveryError:
            pass


@pytest.mark.parametrize("resource_kind", ["writer", "governed-run"])
def test_client_close_drops_owned_event_resources_from_sdk_tracebacks(resource_kind):
    api_key_canary = f"ctk_client_close_{resource_kind}_key_canary"
    event_canary = f"client-close-{resource_kind}-event-canary"
    server_canary = f"client-close-{resource_kind}-server-canary"

    def handler(request):
        import json

        body = json.loads(request.content)
        if body["events"]:
            return httpx.Response(
                503,
                json={"code": "unavailable", "message": server_canary},
                request=request,
            )
        return httpx.Response(
            200,
            json={
                field: [{"id": write["id"], "replayed": False} for write in body[field]]
                for field in ("runs", "events")
            },
            request=request,
        )

    client = Client(
        "https://control.example",
        api_key=api_key_canary,
        agent_name="runtime/worker",
        transport=httpx.MockTransport(handler),
    )
    if resource_kind == "writer":
        client.buffered_observe(
            flush_records=100, flush_interval=60
        ).begin_run().submit({"name": event_canary})
    else:
        from openeight import Event

        run = client.governed_run()
        run.submit_event(
            Event(
                name=event_canary,
                tool_name=event_canary,
                observation_kind="tool_call",
                operation_kind="invoke",
                occurred_at="2026-09-01T12:00:00Z",
                outcome="success",
            )
        )

    with pytest.raises(ObserveDeliveryError) as caught:
        client.close()
    _assert_exported_error_has_no_unsafe_sdk_traceback_state(
        caught.value,
        {api_key_canary, event_canary, server_canary},
    )


def test_arbitrary_transport_exception_is_reduced_without_disclosure(caplog):
    reason_canary = "arbitrary-transport-reason-canary"

    def handler(_: httpx.Request) -> httpx.Response:
        raise RuntimeError(reason_canary)

    client = Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(handler),
    )
    caplog.set_level(logging.DEBUG)
    try:
        with pytest.raises(TransportError) as caught:
            client.health()
    finally:
        client.close()

    error = caught.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert (
        reason_canary not in str(error) + repr(error) + repr(vars(error)) + caplog.text
    )


def test_arbitrary_response_decoder_exception_is_a_safe_protocol_error(caplog):
    reason_canary = "response-decoder-reason-canary"

    class BrokenResponse(httpx.Response):
        def json(self, **kwargs):
            raise RuntimeError(reason_canary)

    client = Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(lambda _: BrokenResponse(502)),
    )
    caplog.set_level(logging.DEBUG)
    try:
        with pytest.raises(ProtocolError) as caught:
            client.health()
    finally:
        client.close()

    error = caught.value
    assert error.status_code == 502
    assert error.__cause__ is None
    assert error.__context__ is None
    assert (
        reason_canary not in str(error) + repr(error) + repr(vars(error)) + caplog.text
    )


def test_request_construction_exception_is_reduced_without_secret_retention(caplog):
    secret_canary = "ctk_secret-💥-canary"
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"runs": [], "events": []})

    client = Client(
        "https://control.example",
        api_key=secret_canary,
        agent_name="runtime/worker",
        transport=httpx.MockTransport(handler),
    )
    caplog.set_level(logging.DEBUG)
    try:
        with pytest.raises(TransportError) as caught:
            client.write_telemetry(runs=[], events=[])
    finally:
        client.close()

    error = caught.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert requests == []
    assert (
        secret_canary not in str(error) + repr(error) + repr(vars(error)) + caplog.text
    )


def test_public_error_constructors_have_fixed_safe_shapes():
    base = SDKError()
    assert base.message == "SDK operation failed"
    assert str(base) == base.message
    assert not hasattr(base, "code")

    configuration = ConfigurationError("not-an-allowlisted-canary")
    assert configuration.reason == "invalid_configuration"
    assert configuration.message == "invalid configuration"
    assert not hasattr(configuration, "code")

    api = APIError(400, "bad_request", "BAD_REQUEST")
    assert (api.status_code, api.category, api.code) == (
        400,
        "bad_request",
        "BAD_REQUEST",
    )
    assert api.message == "request failed"
    with pytest.raises(TypeError):
        APIError(400, "bad_request", "BAD_REQUEST", "server message")
    malformed = APIError(400, {"category-canary": []}, ["code-canary"])
    assert (malformed.category, malformed.code) == ("internal_error", None)
    assert "canary" not in repr(malformed) + repr(vars(malformed))

    protocol = ProtocolError(502)
    transport = TransportError()
    assert protocol.message == "invalid server response"
    assert transport.message == "transport request failed"
    assert not hasattr(protocol, "code")
    assert not hasattr(transport, "code")


def test_blocked_and_observe_errors_are_sdk_errors_with_fixed_safe_state():
    class Result:
        verdict = "deny"
        reason = "deny-reason-secret-canary"

    result = Result()
    blocked = BlockedError(result)
    timeout = ApprovalTimeout(result)
    queue = ObserveQueueFullError("write_queue_full")
    delivery = ObserveDeliveryError("record_too_large")
    completed = ObserveRunCompletedError()

    for error in (blocked, timeout, queue, delivery, completed):
        assert isinstance(error, SDKError)
        assert str(error) == error.message
        assert "deny-reason-secret-canary" not in str(error)
        assert "deny-reason-secret-canary" not in repr(error)
        assert not hasattr(error, "code")

    assert blocked.result is result and blocked.state == "deny"
    assert blocked.message == "action blocked"
    assert timeout.result is result and timeout.state == "approval_wait_timed_out"
    assert timeout.message == "approval timed out"
    assert (queue.reason, queue.message) == (
        "write_queue_full",
        "Observe write queue is full",
    )
    assert (delivery.reason, delivery.message) == (
        "record_too_large",
        "Observe record exceeds server limit",
    )
    assert (completed.reason, completed.message) == (
        "run_completed",
        "Observe Run is completed",
    )


def test_error_repr_contains_only_allowlisted_public_state():
    errors = [
        SDKError(),
        ConfigurationError("invalid_api_key"),
        APIError(401, "unauthorized", "UNAUTHENTICATED"),
        ProtocolError(200),
        TransportError(),
        ObserveQueueFullError("unrecognized-canary"),
        ObserveDeliveryError("unrecognized-canary"),
        ObserveRunCompletedError(),
    ]
    for error in errors:
        representation = repr(error)
        assert type(error).__name__ in representation
        assert error.message in representation
        assert "unrecognized-canary" not in representation


@pytest.mark.parametrize("failure_type", [OSError, RuntimeError, httpx.InvalidURL])
def test_client_setup_failures_are_classified_without_retaining_credentials(
    monkeypatch, failure_type
):
    canaries = (
        "ctk_setup_api_key_canary",
        "setup-endpoint-canary",
        "setup-provider-detail-canary",
    )

    def fail_setup(**kwargs):
        raise failure_type("setup-provider-detail-canary")

    monkeypatch.setattr("openeight.client.httpx.Client", fail_setup)
    expected = (
        ConfigurationError if failure_type is httpx.InvalidURL else TransportError
    )
    with pytest.raises(expected) as caught:
        Client(
            "https://setup-endpoint-canary.example",
            api_key="ctk_setup_api_key_canary",
            agent_name="worker",
        )
    if expected is ConfigurationError:
        assert caught.value.reason == "invalid_base_url"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    _assert_exported_error_has_no_unsafe_sdk_traceback_state(caught.value, canaries)
