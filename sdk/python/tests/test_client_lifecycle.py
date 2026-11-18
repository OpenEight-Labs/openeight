import json
import threading
import time

import httpx
import pytest

from openeight import (
    Client,
    ConfigurationError,
    Event,
    ObserveDeliveryError,
    ProtocolError,
    SDKError,
    TransportError,
)

RUN_ID = "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b12"


def _write_response(request):
    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            field: [{"id": write["id"], "replayed": False} for write in body[field]]
            for field in ("runs", "events")
        },
    )


def _client(handler=None):
    if handler is None:
        handler = _write_response
    return Client(
        "https://control.example",
        api_key="ctk_lifecycle_canary",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(handler),
    )


def _event():
    return Event(
        name="refund.create",
        tool_name="refund.create",
        observation_kind="tool_call",
        operation_kind="invoke",
        occurred_at="2026-09-01T12:00:01Z",
        outcome="success",
    )


def test_run_flush_acknowledges_pending_events_and_remains_live():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return _write_response(request)

    client = _client(handler)
    run = client.governed_run(
        run_id=RUN_ID,
        started_at="2026-09-01T12:00:00Z",
    )

    run.flush()
    run.flush()
    assert len(requests) == 1

    run.submit_event(_event())
    run.flush()
    assert requests[-1]["events"]
    assert requests[-1]["runs"][0]["status"] == "active"
    assert not any(request["runs"][0]["status"] == "completed" for request in requests)

    run.submit_event(_event())
    run.flush()
    run.flush()
    assert sum(bool(request["events"]) for request in requests) == 2

    run.close()
    client.close()


def test_run_flush_failure_is_fresh_cause_free_and_leaves_run_live():
    event_attempts = 0

    def handler(request):
        nonlocal event_attempts
        body = json.loads(request.content)
        if body["events"]:
            event_attempts += 1
            if event_attempts <= 2:
                raise RuntimeError("run-flush-secret-canary")
        return _write_response(request)

    client = _client(handler)
    run = client.governed_run(
        run_id=RUN_ID,
        started_at="2026-09-01T12:00:00Z",
    )
    run.submit_event(_event())

    errors = []
    for _ in range(2):
        with pytest.raises(ObserveDeliveryError) as caught:
            run.flush()
        errors.append(caught.value)

    assert errors[0] is not errors[1]
    assert all(error.reason == "delivery_failed" for error in errors)
    assert all(
        error.__cause__ is None and error.__context__ is None for error in errors
    )
    assert "run-flush-secret-canary" not in repr(errors)
    assert client._owned_runs == {id(run): run}

    run.submit_event(_event())
    run.flush()
    run.suspend()
    client.close()


def test_suspend_flushes_without_completing():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return _write_response(request)

    client = _client(handler)
    run = client.governed_run(
        run_id=RUN_ID,
        started_at="2026-09-01T12:00:00Z",
    )
    run.submit_event(_event())

    run.suspend()

    assert any(body["events"] for body in requests)
    assert not any(
        next(iter(body["runs"]))["status"] == "completed" for body in requests
    )
    assert client._owned_runs == {}
    assert run._client is None
    assert run._writer is None
    assert run._handle is None

    request_count = len(requests)
    run.suspend()
    run.close()
    client.close()
    assert len(requests) == request_count


def test_concurrent_suspend_joins_one_successful_operation():
    delivery_started = threading.Event()
    release_delivery = threading.Event()
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if body["events"]:
            delivery_started.set()
            assert release_delivery.wait(2)
        return _write_response(request)

    client = _client(handler)
    run = client.governed_run(
        run_id=RUN_ID,
        started_at="2026-09-01T12:00:00Z",
    )
    run.submit_event(_event())
    errors = []

    def suspend_run():
        try:
            run.suspend()
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=suspend_run)
    second = threading.Thread(target=suspend_run)
    first.start()
    assert delivery_started.wait(1)
    second.start()
    release_delivery.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert sum(bool(request["events"]) for request in requests) == 1
    assert not any(request["runs"][0]["status"] == "completed" for request in requests)
    client.close()


def test_concurrent_suspend_failure_is_fresh_and_same_handle_retryable():
    delivery_started = threading.Event()
    release_delivery = threading.Event()
    fail_delivery = True

    def handler(request):
        body = json.loads(request.content)
        if body["events"] and fail_delivery:
            delivery_started.set()
            assert release_delivery.wait(2)
            raise RuntimeError("suspend-flush-secret-canary")
        return _write_response(request)

    client = _client(handler)
    run = client.governed_run(
        run_id=RUN_ID,
        started_at="2026-09-01T12:00:00Z",
    )
    run.submit_event(_event())
    errors = []

    def suspend_run():
        try:
            run.suspend()
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=suspend_run)
    second = threading.Thread(target=suspend_run)
    first.start()
    assert delivery_started.wait(1)
    second.start()
    release_delivery.set()
    first.join(2)
    second.join(2)

    assert len(errors) == 2
    assert errors[0] is not errors[1]
    assert all(isinstance(error, ObserveDeliveryError) for error in errors)
    assert all(error.reason == "suspend_flush_failed" for error in errors)
    assert all(
        error.__cause__ is None and error.__context__ is None for error in errors
    )
    assert "suspend-flush-secret-canary" not in repr(errors)
    assert client._owned_runs == {id(run): run}
    assert run._client is client
    assert run._writer is not None
    assert run._handle is not None

    fail_delivery = False
    run.submit_event(_event())
    run.suspend()
    client.close()


def test_suspend_writer_close_failure_requires_fresh_delivery_handle(monkeypatch):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return _write_response(request)

    client = _client(handler)
    run = client.governed_run(
        run_id=RUN_ID,
        started_at="2026-09-01T12:00:00Z",
    )
    run.submit_event(_event())

    def fail_close(*, timeout):
        raise RuntimeError("writer-close-secret-canary")

    monkeypatch.setattr(run._writer, "close", fail_close)
    with pytest.raises(ObserveDeliveryError) as caught:
        run.suspend()

    assert caught.value.reason == "suspend_fresh_delivery_required"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "writer-close-secret-canary" not in repr(caught.value)
    assert client._owned_runs == {}
    assert run._client is None
    assert run._writer is None
    assert run._handle is None
    assert not any(request["runs"][0]["status"] == "completed" for request in requests)

    with pytest.raises(ConfigurationError) as submitted:
        run.submit_event(_event())
    assert submitted.value.reason == "run_closed"
    with pytest.raises(ObserveDeliveryError) as replayed:
        run.suspend()
    assert replayed.value is not caught.value
    assert replayed.value.reason == "suspend_fresh_delivery_required"

    reopened = client.governed_run(
        run_id=RUN_ID,
        started_at="2026-09-01T12:00:00Z",
    )
    reopened.close()
    client.close()
    assert sum(request["runs"][0]["status"] == "completed" for request in requests) == 1


def test_client_close_retries_live_run_after_suspend_flush_failure():
    fail_delivery = True
    requests = []

    def handler(request):
        nonlocal fail_delivery
        body = json.loads(request.content)
        requests.append(body)
        if body["events"] and fail_delivery:
            fail_delivery = False
            raise RuntimeError("suspend-flush-secret-canary")
        return _write_response(request)

    client = _client(handler)
    run = client.governed_run(
        run_id=RUN_ID,
        started_at="2026-09-01T12:00:00Z",
    )
    run.submit_event(_event())

    with pytest.raises(ObserveDeliveryError) as caught:
        run.suspend()
    assert caught.value.reason == "suspend_flush_failed"

    client.close()

    assert client._owned_runs == {}
    assert run._client is None
    assert sum(request["runs"][0]["status"] == "completed" for request in requests) == 1


def test_client_shutdown_prioritizes_settlement_after_joined_suspend_failure(
    monkeypatch,
):
    first_delivery_started = threading.Event()
    release_first_delivery = threading.Event()
    contender_delivery_started = threading.Event()
    release_contender_delivery = threading.Event()
    contender_window = False
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if body["events"]:
            if not first_delivery_started.is_set():
                first_delivery_started.set()
                assert release_first_delivery.wait(2)
                raise RuntimeError("first-suspend-flush-secret-canary")
            if contender_window:
                contender_delivery_started.set()
                assert release_contender_delivery.wait(2)
                raise RuntimeError("contender-suspend-flush-secret-canary")
        return _write_response(request)

    client = _client(handler)
    run = client.governed_run(
        run_id=RUN_ID,
        started_at="2026-09-01T12:00:00Z",
    )
    run.submit_event(_event())
    first_suspend_errors = []
    contender_errors = []
    close_errors = []
    first_client_join_returned = threading.Event()
    allow_client_retry = threading.Event()
    client_retry_started = threading.Event()
    client_close_calls = 0

    real_run_close = run.close

    def controlled_run_close():
        nonlocal client_close_calls
        client_close_calls += 1
        call_number = client_close_calls
        if call_number == 2:
            client_retry_started.set()
        try:
            return real_run_close()
        finally:
            if call_number == 1:
                first_client_join_returned.set()
                assert allow_client_retry.wait(2)

    monkeypatch.setattr(run, "close", controlled_run_close)

    def suspend_run():
        try:
            run.suspend()
        except BaseException as error:
            first_suspend_errors.append(error)

    contender_barrier = threading.Barrier(5)

    def contend_for_suspend():
        contender_barrier.wait()
        try:
            run.suspend()
        except BaseException as error:
            contender_errors.append(error)

    def close_client():
        try:
            client.close()
        except BaseException as error:
            close_errors.append(error)

    first_suspender = threading.Thread(target=suspend_run)
    closer = threading.Thread(target=close_client)
    first_suspender.start()
    assert first_delivery_started.wait(1)
    closer.start()
    deadline = time.monotonic() + 1
    while True:
        with run._close_condition:
            if run._transition_waiters == 1:
                break
        assert time.monotonic() < deadline
        time.sleep(0.005)
    release_first_delivery.set()
    assert first_client_join_returned.wait(1)

    contender_window = True
    contenders = [threading.Thread(target=contend_for_suspend) for _ in range(4)]
    for contender in contenders:
        contender.start()
    contender_barrier.wait()
    deadline = time.monotonic() + 1
    while not contender_delivery_started.is_set() and any(
        contender.is_alive() for contender in contenders
    ):
        assert time.monotonic() < deadline
        time.sleep(0.005)

    contender_window = False
    allow_client_retry.set()
    assert client_retry_started.wait(1)
    release_contender_delivery.set()

    first_suspender.join(2)
    for contender in contenders:
        contender.join(2)
    closer.join(2)

    assert not first_suspender.is_alive()
    assert all(not contender.is_alive() for contender in contenders)
    assert not closer.is_alive()
    assert len(first_suspend_errors) == 1
    assert isinstance(first_suspend_errors[0], ObserveDeliveryError)
    assert first_suspend_errors[0].reason == "suspend_flush_failed"
    assert len(contender_errors) == 4
    assert all(isinstance(error, ConfigurationError) for error in contender_errors)
    assert all(error.reason == "client_closed" for error in contender_errors)
    assert all(
        error.__cause__ is None and error.__context__ is None
        for error in contender_errors
    )
    assert not contender_delivery_started.is_set()
    assert close_errors == []
    assert client._owned_runs == {}
    assert run._client is None
    assert run._writer is None
    assert run._handle is None
    event_requests = [request for request in requests if request["events"]]
    assert len(event_requests) == 2
    assert event_requests[0] == event_requests[1]
    assert sum(request["runs"][0]["status"] == "completed" for request in requests) == 1


def test_suspend_wins_close_race_without_completing_run():
    delivery_started = threading.Event()
    release_delivery = threading.Event()
    requests = []
    errors = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if body["events"]:
            delivery_started.set()
            assert release_delivery.wait(2)
        return _write_response(request)

    client = _client(handler)
    run = client.governed_run(
        run_id=RUN_ID,
        started_at="2026-09-01T12:00:00Z",
    )
    run.submit_event(_event())

    def call(operation):
        try:
            operation()
        except BaseException as error:
            errors.append(error)

    suspender = threading.Thread(target=call, args=(run.suspend,))
    closer = threading.Thread(target=call, args=(run.close,))
    suspender.start()
    assert delivery_started.wait(1)
    closer.start()
    release_delivery.set()
    suspender.join(2)
    closer.join(2)

    assert len(errors) == 1
    assert isinstance(errors[0], ConfigurationError)
    assert not any(request["runs"][0]["status"] == "completed" for request in requests)
    client.close()


def test_close_wins_suspend_race_and_completes_once():
    completion_started = threading.Event()
    release_completion = threading.Event()
    requests = []
    errors = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if body["runs"][0]["status"] == "completed":
            completion_started.set()
            assert release_completion.wait(2)
        return _write_response(request)

    client = _client(handler)
    run = client.governed_run(
        run_id=RUN_ID,
        started_at="2026-09-01T12:00:00Z",
    )

    def call(operation):
        try:
            operation()
        except BaseException as error:
            errors.append(error)

    closer = threading.Thread(target=call, args=(run.close,))
    suspender = threading.Thread(target=call, args=(run.suspend,))
    closer.start()
    assert completion_started.wait(1)
    suspender.start()
    release_completion.set()
    closer.join(2)
    suspender.join(2)

    assert len(errors) == 1
    assert isinstance(errors[0], ConfigurationError)
    assert sum(request["runs"][0]["status"] == "completed" for request in requests) == 1
    client.close()


def test_client_close_flushes_open_run_before_completion_then_closes_direct_writer():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return _write_response(request)

    client = _client(handler)
    run = client.governed_run(
        run_id=RUN_ID,
        started_at="2026-09-01T12:00:00Z",
    )
    run.submit_event(_event())
    writer = client.buffered_observe(flush_records=1000)
    standalone = writer.begin_run()
    standalone.submit({"name": "standalone"})

    client.close()

    assert [request["runs"][0]["status"] for request in requests] == [
        "active",
        "active",
        "completed",
        "active",
    ]
    assert requests[1]["events"][0]["run_id"] == RUN_ID
    assert requests[1]["runs"][0]["started_at"] == "2026-09-01T12:00:00.000000Z"
    assert requests[2]["runs"][0] == {
        "id": RUN_ID,
        "status": "completed",
        "started_at": "2026-09-01T12:00:00.000000Z",
        "ended_at": requests[2]["runs"][0]["ended_at"],
        "attributes_json": "{}",
    }
    with pytest.raises(ConfigurationError) as run_error:
        run.submit_event(_event())
    assert run_error.value.reason == "run_closed"
    with pytest.raises(ConfigurationError) as writer_error:
        writer.begin_run()
    assert writer_error.value.reason == "writer_closed"


def test_buffered_writer_retains_chunk_until_exact_acknowledgement_is_validated():
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "runs": [{"id": body["runs"][0]["id"], "replayed": False}],
                    "events": [{"id": "wrong-event-id", "replayed": False}],
                },
            )
        return _write_response(request)

    client = _client(handler)
    writer = client.buffered_observe(flush_records=1000)
    writer.begin_run().submit({"name": "queued"})

    with pytest.raises(ObserveDeliveryError) as caught:
        writer.flush()
    assert caught.value.reason == "delivery_failed"

    writer.flush()
    assert requests[0] == requests[1]
    writer.close()
    client.close()


def test_governed_run_completion_requires_an_exact_acknowledgement():
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if body["runs"][0]["status"] == "completed":
            return httpx.Response(
                200,
                json={"runs": [], "events": [], "agent_id": "server-owned"},
            )
        return _write_response(request)

    client = _client(handler)
    run = client.governed_run(
        run_id=RUN_ID,
        started_at="2026-09-01T12:00:00Z",
    )

    with pytest.raises(ProtocolError) as caught:
        run.close()

    assert caught.value.status_code == 200
    assert [request["runs"][0]["status"] for request in requests] == [
        "active",
        "completed",
    ]
    with pytest.raises(ProtocolError):
        run.close()
    with pytest.raises(ProtocolError):
        client.close()


def test_run_close_completes_after_cleanup_retry_delivers_initially_failed_events():
    requests = []
    event_attempts = 0

    def handler(request):
        nonlocal event_attempts
        body = json.loads(request.content)
        requests.append(body)
        if body["events"]:
            event_attempts += 1
            if event_attempts == 1:
                return httpx.Response(
                    503,
                    json={"code": "unavailable", "message": "temporarily unavailable"},
                )
        return _write_response(request)

    client = _client(handler)
    run = client.governed_run(
        run_id=RUN_ID,
        started_at="2026-09-01T12:00:00Z",
    )
    run.submit_event(_event())

    with pytest.raises(ObserveDeliveryError) as caught:
        run.close()
    assert caught.value.reason == "delivery_failed"

    assert [
        (request["runs"][0]["status"], bool(request["events"])) for request in requests
    ] == [("active", False), ("active", True)]
    run.close()
    assert [
        (request["runs"][0]["status"], bool(request["events"])) for request in requests
    ] == [("active", False), ("active", True), ("active", True), ("completed", False)]
    run.close()
    client.close()


def test_terminal_resources_release_client_ownership_and_are_not_closed_twice():
    client = _client()
    writer = client.buffered_observe()
    writer.close()
    run = client.governed_run(
        run_id=RUN_ID,
        started_at="2026-09-01T12:00:00Z",
    )
    run.close()

    def should_not_run():
        pytest.fail("terminal resource remained owned by the client")

    writer.close = should_not_run
    run.close = should_not_run
    client.close()


def test_simultaneous_run_close_joins_one_operation_and_replays_fresh_safe_error():
    delivery_started = threading.Event()
    release_delivery = threading.Event()

    def handler(request):
        body = json.loads(request.content)
        if body.get("events"):
            delivery_started.set()
            assert release_delivery.wait(2)
            raise RuntimeError("run-close-secret-canary")
        return _write_response(request)

    client = _client(handler)
    run = client.governed_run(
        run_id=RUN_ID,
        started_at="2026-09-01T12:00:00Z",
    )
    run.submit_event(_event())
    errors = []

    def close_run():
        try:
            run.close()
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=close_run)
    second = threading.Thread(target=close_run)
    first.start()
    assert delivery_started.wait(1)
    second.start()
    time.sleep(0.02)
    release_delivery.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive() and not second.is_alive()
    assert len(errors) == 2
    assert all(isinstance(error, ObserveDeliveryError) for error in errors)
    assert errors[0] is not errors[1]
    assert all(error.reason == "delivery_failed" for error in errors)
    assert all(
        error.__cause__ is None and error.__context__ is None for error in errors
    )
    assert "run-close-secret-canary" not in repr(errors)

    with pytest.raises(ObserveDeliveryError) as replayed:
        run.close()
    assert replayed.value is not errors[0] and replayed.value is not errors[1]
    assert replayed.value.reason == "delivery_failed"
    with pytest.raises(ObserveDeliveryError):
        client.close()


class _FailingResource:
    def __init__(self, calls, name, error=None, on_close=None):
        self.calls = calls
        self.name = name
        self.error = error
        self.on_close = on_close
        self.release = None

    def _bind_owner(self, release):
        self.release = release

    def close(self):
        self.calls.append(self.name)
        if self.on_close is not None:
            self.on_close()
        if self.error is not None:
            raise self.error


class _CloseFailureTransport(httpx.BaseTransport):
    def __init__(self, calls):
        self.calls = calls

    def handle_request(self, request):
        return _write_response(request)

    def close(self):
        self.calls.append("http")
        raise RuntimeError("http-close-secret-canary")


def test_client_close_snapshots_outside_lock_attempts_all_and_keeps_fixed_precedence():
    calls = []
    client = Client(
        "https://control.example",
        api_key="ctk_lifecycle_canary",
        agent_name="runtime/worker",
        transport=_CloseFailureTransport(calls),
    )

    governed = _FailingResource(
        calls,
        "governed",
        ObserveDeliveryError("record_too_large"),
    )
    writer = _FailingResource(
        calls,
        "writer",
        ObserveDeliveryError("flush_timeout"),
    )
    client._register_owned_run(governed)
    client._register_owned_writer(writer)

    with pytest.raises(ObserveDeliveryError) as first:
        client.close()
    assert first.value.reason == "record_too_large"
    assert first.value.__cause__ is None and first.value.__context__ is None
    assert calls == ["governed", "writer", "http"]

    with pytest.raises(ObserveDeliveryError) as second:
        client.close()
    assert second.value.reason == "record_too_large"
    assert second.value is not first.value
    assert calls == ["governed", "writer", "http"]


def test_close_racing_governed_run_creation_never_returns_a_live_unowned_run():
    active_started = threading.Event()
    release_active = threading.Event()
    created = []
    errors = []

    def handler(request):
        body = json.loads(request.content)
        if body.get("runs", [{}])[0].get("status") == "active" and not body.get(
            "events"
        ):
            active_started.set()
            assert release_active.wait(2)
        return _write_response(request)

    client = _client(handler)

    def create_run():
        try:
            created.append(client.governed_run(run_id=RUN_ID))
        except BaseException as error:
            errors.append(error)

    creator = threading.Thread(target=create_run)
    creator.start()
    assert active_started.wait(1)
    closer = threading.Thread(target=client.close)
    closer.start()
    release_active.set()
    creator.join(2)
    closer.join(2)

    assert not creator.is_alive() and not closer.is_alive()
    if created:
        with pytest.raises(ConfigurationError) as caught:
            created[0].submit_event(_event())
        assert caught.value.reason == "run_closed"
    else:
        assert len(errors) == 1
        assert isinstance(errors[0], ConfigurationError)
        assert errors[0].reason == "client_closed"


def test_run_close_waits_for_submission_already_in_progress_and_flushes_it_first():
    requests = []
    submit_started = threading.Event()
    release_submit = threading.Event()
    close_done = threading.Event()
    submit_errors = []

    def handler(request):
        requests.append(json.loads(request.content))
        return _write_response(request)

    client = _client(handler)
    run = client.governed_run(
        run_id=RUN_ID,
        started_at="2026-09-01T12:00:00Z",
    )
    original_submit = run._handle.submit

    def blocking_submit(*args, **kwargs):
        submit_started.set()
        assert release_submit.wait(2)
        return original_submit(*args, **kwargs)

    run._handle.submit = blocking_submit

    def submit():
        try:
            run.submit_event(_event())
        except BaseException as error:
            submit_errors.append(error)

    submitter = threading.Thread(target=submit)
    submitter.start()
    assert submit_started.wait(1)
    closer = threading.Thread(target=lambda: (run.close(), close_done.set()))
    closer.start()
    assert close_done.wait(0.02) is False
    release_submit.set()
    submitter.join(2)
    closer.join(2)

    assert submit_errors == []
    assert close_done.is_set()
    assert [request["runs"][0]["status"] for request in requests] == [
        "active",
        "active",
        "completed",
    ]
    assert requests[1]["events"][0]["run_id"] == RUN_ID
    client.close()


def test_blocking_http_close_is_bounded_and_replays_transport_failure(monkeypatch):
    release = threading.Event()

    class BlockingTransport(httpx.BaseTransport):
        def handle_request(self, request):
            return httpx.Response(200, json={"status": "ok"})

        def close(self):
            release.wait(2)

    monkeypatch.setattr("openeight.client._CLOSE_TIMEOUT_SECONDS", 0.02)
    client = Client(
        "https://control.example",
        api_key="ctk_lifecycle_canary",
        agent_name="runtime/worker",
        transport=BlockingTransport(),
    )
    started = time.monotonic()
    try:
        with pytest.raises(TransportError):
            client.close()
        assert time.monotonic() - started < 0.5
        with pytest.raises(TransportError):
            client.close()
    finally:
        release.set()


def test_client_close_bounds_each_owned_resource_and_attempts_later_cleanup(
    monkeypatch,
):
    calls = []
    release = threading.Event()
    monkeypatch.setattr("openeight.client._CLOSE_TIMEOUT_SECONDS", 0.02)
    client = _client()

    class NeverReturningResource(_FailingResource):
        def close(self):
            self.calls.append(self.name)
            release.wait()

    governed = NeverReturningResource(calls, "governed")
    writer = _FailingResource(calls, "writer")
    client._register_owned_run(governed)
    client._register_owned_writer(writer)

    started = time.monotonic()
    try:
        with pytest.raises(SDKError):
            client.close()
        assert time.monotonic() - started < 0.5
        assert calls == ["governed", "writer"]
    finally:
        release.set()


def test_governed_run_close_is_bounded_when_completion_transport_never_returns(
    monkeypatch,
):
    completion_started = threading.Event()
    release = threading.Event()
    monkeypatch.setattr("openeight.run._CLOSE_TIMEOUT_SECONDS", 0.02)

    def handler(request):
        body = json.loads(request.content)
        if body["runs"][0]["status"] == "completed":
            completion_started.set()
            release.wait()
        return _write_response(request)

    client = _client(handler)
    run = client.governed_run(
        run_id=RUN_ID,
        started_at="2026-09-01T12:00:00Z",
    )

    started = time.monotonic()
    try:
        with pytest.raises(TransportError):
            run.close()
        assert time.monotonic() - started < 0.5
        assert completion_started.is_set()
    finally:
        release.set()
        try:
            client.close()
        except (TransportError, ConfigurationError):
            pass


@pytest.mark.parametrize("resource_kind", ["writer", "governed-run"])
def test_client_close_scrubs_sdk_ownership_while_foreign_sender_remains_blocked(
    monkeypatch,
    resource_kind,
):
    callback_started = threading.Event()
    release_callback = threading.Event()

    def handler(request):
        body = json.loads(request.content)
        if body["events"]:
            callback_started.set()
            release_callback.wait()
        return _write_response(request)

    monkeypatch.setattr("openeight.client._CLOSE_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr("openeight.run._CLOSE_TIMEOUT_SECONDS", 0.02)
    client = _client(handler)
    run = None
    if resource_kind == "writer":
        writer = client.buffered_observe(flush_records=100, flush_interval=60)
        writer.begin_run().submit({"name": "blocked-foreign-callback"})
    else:
        run = client.governed_run(
            run_id=RUN_ID,
            started_at="2026-09-01T12:00:00Z",
        )
        writer = run._writer
        run.submit_event(_event())

    try:
        with pytest.raises(ObserveDeliveryError) as caught:
            client.close()
        assert caught.value.reason == "flush_timeout"
        assert callback_started.is_set()
        assert client._owned_runs == {}
        assert client._owned_writers == {}
        assert client._http is None
        assert client._api_key == ""
        assert writer._pending == []
        assert writer._failed == []
        assert writer._active_runs == {}
        assert writer._owner_release is None
        assert writer._send is None
        assert writer._send_completed is None
        if run is not None:
            assert run._client is None
            assert run._writer is None
            assert run._handle is None
    finally:
        release_callback.set()


def test_client_close_clears_retained_machine_identity_material():
    client = _client()
    assert client._api_key == "ctk_lifecycle_canary"
    assert client._agent_name == "runtime/worker"

    client.close()

    assert client._api_key == ""


def test_writer_context_exit_preserves_body_exception_when_close_fails():
    cleanup_canary = "writer-cleanup-secret-canary"

    def handler(request):
        body = json.loads(request.content)
        if body.get("events"):
            raise RuntimeError(cleanup_canary)
        return _write_response(request)

    client = _client(handler)
    writer = client.buffered_observe()
    writer.begin_run().submit({"name": "standalone"})
    body_error = RuntimeError("protected-writer-body")

    with pytest.raises(RuntimeError) as caught:
        with writer:
            raise body_error

    assert caught.value is body_error
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert cleanup_canary not in repr(caught.value) + repr(vars(caught.value))
    client.close()


def test_run_context_exit_preserves_body_exception_when_close_fails():
    cleanup_canary = "run-cleanup-secret-canary"

    def handler(request):
        body = json.loads(request.content)
        if body.get("events"):
            raise RuntimeError(cleanup_canary)
        return _write_response(request)

    client = _client(handler)
    run = client.governed_run(
        run_id=RUN_ID,
        started_at="2026-09-01T12:00:00Z",
    )
    run.submit_event(_event())
    body_error = RuntimeError("protected-run-body")

    with pytest.raises(RuntimeError) as caught:
        with run:
            raise body_error

    assert caught.value is body_error
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert cleanup_canary not in repr(caught.value) + repr(vars(caught.value))
    with pytest.raises(ObserveDeliveryError):
        client.close()
    assert client._agent_name == ""


def test_client_close_waits_for_authenticated_header_snapshot_before_erasing_secret():
    header_snapshot_started = threading.Event()
    release_snapshot = threading.Event()
    close_done = threading.Event()
    requests = []
    errors = []

    def handler(request):
        requests.append(request)
        return _write_response(request)

    client = _client(handler)
    original_headers = client._machine_headers

    def blocking_headers():
        header_snapshot_started.set()
        assert release_snapshot.wait(2)
        return original_headers()

    client._machine_headers = blocking_headers

    def write():
        try:
            client.write_telemetry(runs=[], events=[])
        except BaseException as error:
            errors.append(error)

    sender = threading.Thread(target=write)
    sender.start()
    assert header_snapshot_started.wait(1)
    closer = threading.Thread(target=lambda: (client.close(), close_done.set()))
    closer.start()
    assert close_done.wait(0.02) is False
    release_snapshot.set()
    sender.join(2)
    closer.join(2)

    assert errors == []
    assert close_done.is_set()
    assert requests[0].headers["Authorization"] == "ApiKey ctk_lifecycle_canary"
    assert client._api_key == ""


@pytest.mark.parametrize("phase", ["build", "send"])
def test_close_drains_admitted_request_before_closing_transport(monkeypatch, phase):
    entered, release, draining = (threading.Event() for _ in range(3))
    transport_closed = threading.Event()
    errors = []
    requests = []

    def handler(request):
        requests.append(request)
        return _write_response(request)

    client = _client(handler)
    http = client._http
    original = http.build_request if phase == "build" else http.send
    original_wait = client._close_condition.wait
    original_close = http.close

    def hold(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    def wait(timeout=None):
        draining.set()
        return original_wait(timeout)

    def close_transport():
        transport_closed.set()
        original_close()

    monkeypatch.setattr(http, "build_request" if phase == "build" else "send", hold)
    monkeypatch.setattr(http, "close", close_transport)
    monkeypatch.setattr(client._close_condition, "wait", wait)

    def invoke(operation):
        try:
            operation()
        except BaseException as error:
            errors.append(error)

    sender = threading.Thread(
        target=lambda: invoke(lambda: client.write_telemetry(runs=[], events=[]))
    )
    closer = threading.Thread(target=lambda: invoke(client.close))
    sender.start()
    try:
        assert entered.wait(2)
        closer.start()
        assert draining.wait(2)
        assert not transport_closed.is_set()
        with pytest.raises(ConfigurationError) as caught:
            client.health()
        assert caught.value.reason == "client_closed"
    finally:
        release.set()
        sender.join(5)
        if closer.ident is not None:
            closer.join(5)
        client.close()
    assert not sender.is_alive() and not closer.is_alive()
    assert errors == []
    assert transport_closed.is_set()
    assert requests[0].headers["Authorization"] == "ApiKey ctk_lifecycle_canary"
    assert client._api_key == ""


@pytest.mark.parametrize("adapter_owned", [False, True])
def test_close_bounds_admitted_request_and_prevents_late_transport_creation(
    monkeypatch, adapter_owned
):
    entered, release = threading.Event(), threading.Event()
    errors = []
    client = _client()
    client._adapter_owned_send = adapter_owned
    original = client._http.build_request
    monkeypatch.setattr("openeight.client._CLOSE_TIMEOUT_SECONDS", 0.02)

    def hold(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(client._http, "build_request", hold)

    def send():
        try:
            client.write_telemetry(runs=[], events=[])
        except BaseException as error:
            errors.append(error)

    sender = threading.Thread(target=send)
    sender.start()
    try:
        assert entered.wait(2)
        started = time.monotonic()
        with pytest.raises(TransportError):
            client.close()
        assert time.monotonic() - started < 0.5
        assert client._api_key == ""
        assert client._http is None and client._adapter_http is None
        with pytest.raises(TransportError):
            client.close()
    finally:
        release.set()
        sender.join(5)
    assert not sender.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], TransportError)
    assert client._active_requests == 0
    assert client._adapter_http is None
