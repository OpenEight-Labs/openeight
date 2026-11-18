import json
import threading
import time
import uuid

import httpx
import pytest

import openeight.telemetry as telemetry
from openeight import Client, ConfigurationError, ObserveRunHandle
from openeight._forbidden_fields import FORBIDDEN_TELEMETRY_SELECTORS
from openeight.telemetry import (
    BufferedObserveWriter,
    ObserveDeliveryError,
    ObserveQueueFullError,
    ObserveRunCompletedError,
    uuid7,
)


def _acknowledge(request):
    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            field: [{"id": write["id"], "replayed": False} for write in body[field]]
            for field in ("runs", "events")
        },
    )


def test_uuid7_has_version_and_variant_bits():
    value = uuid7()
    assert value[14] == "7"
    assert value[19] in "89ab"


def test_run_handle_rejects_post_terminal_submissions_and_evicts_writer_state():
    calls = []
    writer = BufferedObserveWriter(
        lambda run_id, events, _timeout: calls.append((run_id, events)),
    )
    run = writer.begin_run()

    assert isinstance(run, ObserveRunHandle)
    assert uuid.UUID(run.run_id).version == 7
    run.submit({"name": "terminal"}, completed=True)

    assert len(calls) == 1
    assert calls[0][0] == run.run_id
    assert writer._active_runs == {}
    with pytest.raises(ObserveRunCompletedError, match="Observe Run is completed"):
        run.submit({"name": "late"})


def test_completed_run_handles_do_not_accumulate_central_metadata():
    writer = BufferedObserveWriter(lambda *_: None)

    for _ in range(100):
        writer.begin_run().submit({"name": "terminal"}, completed=True)

    assert writer._active_runs == {}


def test_begin_run_bounds_active_lifecycles_and_terminal_releases_capacity():
    writer = BufferedObserveWriter(lambda *_: None, max_records=2)
    first = writer.begin_run()
    writer.begin_run()

    with pytest.raises(ObserveQueueFullError, match="Run registry is full"):
        writer.begin_run()

    first.submit({"name": "terminal"}, completed=True)
    replacement = writer.begin_run()

    assert replacement.run_id in writer._active_runs
    assert len(writer._active_runs) == 2


@pytest.mark.parametrize("run_id", ["", "not-a-uuid", str(uuid.uuid4())])
def test_begin_run_rejects_invalid_or_non_v7_caller_selected_ids(run_id):
    writer = BufferedObserveWriter(lambda *_: None)

    with pytest.raises(ConfigurationError) as caught:
        writer.begin_run(run_id)
    assert caught.value.reason == "invalid_run_id"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_evicted_caller_selected_run_id_is_left_to_durable_server_conflict():
    calls = []
    writer = BufferedObserveWriter(
        lambda run_id, events, _timeout: calls.append((run_id, events)),
    )
    run_id = uuid7()

    writer.begin_run(run_id).submit({"name": "first"}, completed=True)
    writer.begin_run(run_id).submit({"name": "historical-reuse"}, completed=True)

    assert [call[0] for call in calls] == [run_id, run_id]
    assert writer._active_runs == {}


def test_terminal_run_handle_retains_retry_state_until_delivery_succeeds():
    calls = []
    fail = True

    def send(run_id, events, _timeout):
        nonlocal fail
        calls.append((run_id, events))
        if fail:
            raise RuntimeError("offline")

    writer = BufferedObserveWriter(send)
    run = writer.begin_run()

    with pytest.raises(ObserveDeliveryError):
        run.submit({"name": "terminal"}, completed=True)

    assert list(writer._active_runs) == [run.run_id]
    with pytest.raises(ObserveRunCompletedError, match="Observe Run is completed"):
        run.submit({"name": "late"})

    fail = False
    writer.flush()

    assert writer._active_runs == {}
    assert calls[0] == calls[1]


def test_overlapping_active_and_terminal_flushes_preserve_delivery_order():
    delivery_order = []
    errors = []
    active_delivery_started = threading.Event()
    release_active_delivery = threading.Event()
    terminal_submit_started = threading.Event()
    terminal_delivery_started = threading.Event()

    def send_active(run_id, events, _timeout):
        active_delivery_started.set()
        if not release_active_delivery.wait(2):
            raise RuntimeError("active delivery was never released")
        delivery_order.append((run_id, events[0]["name"]))

    def send_terminal(run_id, events, ended_at, _timeout):
        terminal_delivery_started.set()
        delivery_order.append((run_id, events[0]["name"]))

    writer = BufferedObserveWriter(
        send_active,
        send_completed=send_terminal,
        flush_records=10_000,
    )
    run = writer.begin_run()
    run.submit({"name": "active"})

    def flush_active():
        try:
            writer.flush()
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def submit_terminal():
        terminal_submit_started.set()
        try:
            run.submit({"name": "terminal"}, completed=True)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    active_thread = threading.Thread(target=flush_active)
    active_thread.start()
    assert active_delivery_started.wait(1)

    terminal_thread = threading.Thread(target=submit_terminal)
    terminal_thread.start()
    assert terminal_submit_started.wait(1)
    terminal_overtook_active = terminal_delivery_started.wait(1)
    release_active_delivery.set()

    active_thread.join(2)
    terminal_thread.join(2)

    assert not active_thread.is_alive()
    assert not terminal_thread.is_alive()
    assert errors == []
    assert terminal_overtook_active is False
    assert delivery_order == [
        (run.run_id, "active"),
        (run.run_id, "terminal"),
    ]
    assert writer._active_runs == {}


def test_buffered_writer_flushes_by_run_and_retains_failed_operations():
    calls = []
    fail = True

    def send(run_id, events, _timeout):
        nonlocal fail
        calls.append((run_id, events))
        if fail:
            raise RuntimeError("offline")

    writer = BufferedObserveWriter(send, max_records=2)
    run = writer.begin_run()
    run.submit({"name": "tool"})
    with pytest.raises(ObserveDeliveryError):
        writer.flush()
    fail = False
    writer.flush()
    assert len(calls) == 2
    assert [call[0] for call in calls] == [run.run_id, run.run_id]
    assert calls[0][1][0]["name"] == "tool"
    assert calls[1][1] == calls[0][1]


def test_buffered_writer_retains_later_runs_when_an_earlier_run_fails():
    calls = []
    fail = True

    def send(run_id, events, _timeout):
        nonlocal fail
        calls.append((run_id, events))
        if fail:
            raise RuntimeError("offline")

    writer = BufferedObserveWriter(send, flush_records=10_000)
    first_run = writer.begin_run()
    second_run = writer.begin_run()
    first_run.submit({"name": "first"})
    second_run.submit({"name": "second"})

    with pytest.raises(ObserveDeliveryError):
        writer.flush()

    fail = False
    writer.flush()

    assert [run_id for run_id, _ in calls] == [
        first_run.run_id,
        first_run.run_id,
        second_run.run_id,
    ]
    assert calls[-1][1][0]["name"] == "second"


def test_buffered_writer_preserves_global_acceptance_order_across_run_segments():
    calls = []
    writer = BufferedObserveWriter(
        lambda run_id, events, _timeout: calls.append(
            (run_id, [event["name"] for event in events])
        ),
        flush_records=10_000,
        flush_interval=60,
    )
    first_run = writer.begin_run()
    second_run = writer.begin_run()
    first_run.submit({"name": "first-a"})
    second_run.submit({"name": "second"})
    first_run.submit({"name": "first-b"})

    writer.flush()

    assert calls == [
        (first_run.run_id, ["first-a"]),
        (second_run.run_id, ["second"]),
        (first_run.run_id, ["first-b"]),
    ]


def test_failed_segment_retry_preserves_exact_global_acceptance_order():
    calls = []
    fail_second_run = True

    def send(run_id, events, _timeout):
        nonlocal fail_second_run
        calls.append((run_id, [event["name"] for event in events]))
        if run_id == second_run.run_id and fail_second_run:
            raise RuntimeError("offline")

    writer = BufferedObserveWriter(
        send,
        flush_records=10_000,
        flush_interval=60,
    )
    first_run = writer.begin_run()
    second_run = writer.begin_run()
    first_run.submit({"name": "first-a"})
    second_run.submit({"name": "second"})
    first_run.submit({"name": "first-b"})

    with pytest.raises(ObserveDeliveryError):
        writer.flush()
    fail_second_run = False
    writer.flush()

    assert calls == [
        (first_run.run_id, ["first-a"]),
        (second_run.run_id, ["second"]),
        (second_run.run_id, ["second"]),
        (first_run.run_id, ["first-b"]),
    ]


def test_buffered_writer_counts_failed_records_toward_byte_limit():
    event = {"name": "retained"}
    event_size = len(
        json.dumps({**event, "event_id": "0" * 36}, separators=(",", ":")).encode()
    )
    writer = BufferedObserveWriter(
        lambda *_: (_ for _ in ()).throw(RuntimeError("offline")),
        max_bytes=event_size + 1,
        flush_records=1000,
    )
    run = writer.begin_run()
    run.submit(event)

    with pytest.raises(ObserveDeliveryError):
        writer.flush()

    with pytest.raises(ObserveQueueFullError):
        run.submit({"name": "x"})


def test_inflight_record_reservation_prevents_concurrent_queue_overcommit():
    delivery_started = threading.Event()
    release_delivery = threading.Event()
    delivery_errors = []

    def send(_run_id, _events, _timeout):
        delivery_started.set()
        assert release_delivery.wait(2)
        raise RuntimeError("offline")

    writer = BufferedObserveWriter(
        send,
        max_records=1,
        flush_records=10_000,
        flush_interval=60,
    )
    run = writer.begin_run()
    run.submit({"name": "reserved"})

    def flush():
        try:
            writer.flush()
        except Exception as exc:  # pragma: no cover - asserted below
            delivery_errors.append(exc)

    thread = threading.Thread(target=flush)
    thread.start()
    assert delivery_started.wait(1)

    with pytest.raises(ObserveQueueFullError):
        run.submit({"name": "must-not-fit"})

    release_delivery.set()
    thread.join(2)
    assert not thread.is_alive()
    assert len(delivery_errors) == 1
    assert isinstance(delivery_errors[0], ObserveDeliveryError)


def test_inflight_byte_reservation_prevents_concurrent_queue_overcommit(monkeypatch):
    delivery_started = threading.Event()
    release_delivery = threading.Event()
    delivery_errors = []
    event_id = "01890f3e-7b2a-7cc0-8f6a-123456789abd"
    event = {"name": "x" * 80}
    monkeypatch.setattr(telemetry, "uuid7", lambda: event_id)

    def send(_run_id, _events, _timeout):
        delivery_started.set()
        assert release_delivery.wait(2)
        raise RuntimeError("offline")

    writer = BufferedObserveWriter(
        send,
        max_records=10,
        max_bytes=200,
        flush_records=10,
        flush_interval=60,
    )
    run = writer.begin_run("01890f3e-7b2a-7cc0-8f6a-123456789abc")
    run.submit(event)

    def flush():
        try:
            writer.flush()
        except Exception as exc:  # pragma: no cover - asserted below
            delivery_errors.append(exc)

    thread = threading.Thread(target=flush)
    thread.start()
    assert delivery_started.wait(1)

    with pytest.raises(ObserveQueueFullError):
        run.submit(event)

    release_delivery.set()
    thread.join(2)
    assert not thread.is_alive()
    assert len(delivery_errors) == 1
    assert isinstance(delivery_errors[0], ObserveDeliveryError)


def test_buffered_writer_timeout_retains_all_runs_in_order():
    calls = []
    writer = BufferedObserveWriter(
        lambda run_id, events, _timeout: calls.append((run_id, events)),
        flush_records=10_000,
    )
    first_run = writer.begin_run()
    second_run = writer.begin_run()
    first_run.submit({"name": "first"})
    second_run.submit({"name": "second"})

    with pytest.raises(ConfigurationError) as caught:
        writer.flush(timeout=0)
    assert caught.value.reason == "invalid_timeout"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None

    writer.flush()
    assert [run_id for run_id, _ in calls] == [first_run.run_id, second_run.run_id]


def test_flush_propagates_decreasing_remaining_timeout_to_each_delivery():
    timeouts = []

    def send(_run_id, _events, timeout):
        timeouts.append(timeout)
        if len(timeouts) == 1:
            time.sleep(0.02)

    writer = BufferedObserveWriter(
        send,
        flush_records=10_000,
        max_request_records=2,
    )
    run = writer.begin_run()
    run.submit({"name": "first"})
    run.submit({"name": "second"})

    writer.flush(timeout=0.5)

    assert len(timeouts) == 2
    assert 0 < timeouts[1] < timeouts[0] <= 0.5


def test_flush_never_retries_a_sender_without_the_timeout_contract():
    calls = []

    def send(_run_id, _events):
        calls.append("delivered-without-timeout")

    writer = BufferedObserveWriter(send, flush_records=10_000)
    writer.begin_run().submit({"name": "missing-timeout"})

    with pytest.raises(ObserveDeliveryError, match="delivery failed"):
        writer.flush(timeout=0.25)

    assert calls == []


def test_completed_delivery_never_retries_without_the_timeout_contract():
    calls = []

    def send_completed(_run_id, _events, _ended_at):
        calls.append("delivered-without-timeout")

    writer = BufferedObserveWriter(
        lambda *_: pytest.fail("active sender used for completed Run"),
        send_completed=send_completed,
    )

    with pytest.raises(ObserveDeliveryError, match="delivery failed"):
        writer.begin_run().submit({"name": "terminal"}, completed=True)

    assert calls == []


def test_flush_reports_timeout_when_sender_returns_after_deadline():
    attempts = []

    def send(_run_id, events, timeout):
        attempts.append([event["name"] for event in events])
        time.sleep(timeout + 0.02)

    writer = BufferedObserveWriter(send, flush_records=10_000)
    writer.begin_run().submit({"name": "late-success"})

    with pytest.raises(ObserveDeliveryError, match="timed out"):
        writer.flush(timeout=0.01)

    assert attempts == [["late-success"]]


def test_writer_close_has_a_wall_clock_deadline_when_sender_never_returns():
    sender_started = threading.Event()
    release_sender = threading.Event()

    def send(_run_id, _events, _timeout):
        sender_started.set()
        release_sender.wait()

    writer = BufferedObserveWriter(send, flush_records=10_000)
    writer.begin_run().submit({"name": "never-returning"})

    started = time.monotonic()
    try:
        with pytest.raises(ObserveDeliveryError) as caught:
            writer.close(timeout=0.02)
        assert caught.value.reason == "flush_timeout"
        assert time.monotonic() - started < 0.5
        assert sender_started.is_set()
    finally:
        release_sender.set()


def test_buffered_writer_oversized_record_retains_it_and_later_runs():
    writer = BufferedObserveWriter(
        lambda *_: None,
        max_records=2,
        max_request_bytes=32,
        flush_records=10_000,
    )
    writer.begin_run().submit({"name": "x" * 100})
    writer.begin_run().submit({"name": "later"})

    with pytest.raises(ObserveDeliveryError, match="exceeds server limit"):
        writer.flush()

    with pytest.raises(ObserveQueueFullError):
        writer.begin_run().submit({"name": "not accepted"})


def test_buffered_writer_rejects_queue_overflow_without_retaining_record():
    writer = BufferedObserveWriter(lambda *_: None, max_records=1)
    run = writer.begin_run()
    run.submit({"name": "first"})
    with pytest.raises(ObserveQueueFullError):
        run.submit({"name": "second"})


def test_buffered_writer_flushes_at_record_threshold_and_keeps_runs_atomic():
    calls = []
    writer = BufferedObserveWriter(
        lambda run_id, events, _timeout: calls.append((run_id, events)),
        flush_records=2,
    )
    run = writer.begin_run()

    run.submit({"name": "first"})
    assert calls == []
    run.submit({"name": "second"})
    assert len(calls) == 1
    assert calls[0][0] == run.run_id
    assert [event["name"] for event in calls[0][1]] == ["first", "second"]


def test_buffered_writer_splits_large_run_batches_before_server_limit():
    calls = []
    writer = BufferedObserveWriter(
        lambda run_id, events, _timeout: calls.append((run_id, events)),
        flush_records=10_000,
        max_request_records=2,
    )
    run = writer.begin_run()
    for i in range(5):
        run.submit({"name": str(i)})

    writer.flush()
    assert [len(events) for _, events in calls] == [1, 1, 1, 1, 1]


def test_buffered_writer_caps_combined_run_and_event_records_at_1000():
    batch_sizes = []
    writer = BufferedObserveWriter(
        lambda _run_id, events, _timeout: batch_sizes.append(len(events)),
        max_records=1000,
        flush_records=10_000,
        max_request_records=1000,
    )
    run = writer.begin_run()
    for index in range(1000):
        run.submit({"name": str(index)})

    writer.flush()

    assert batch_sizes == [999, 1]


def test_buffered_writer_allocates_immutable_event_id_before_delivery():
    sent = []
    writer = BufferedObserveWriter(lambda _, events, _timeout: sent.extend(events))
    operation = writer.begin_run().submit({"name": "tool"})
    writer.flush()

    assert operation.event_id
    assert sent[0]["event_id"] == operation.event_id
    assert sent[0]["name"] == "tool"


def test_accepted_operation_is_unchanged_by_caller_mutation():
    sent = []
    writer = BufferedObserveWriter(
        lambda _run_id, events, _timeout: sent.extend(events),
        flush_records=10_000,
    )
    source = {"name": "original", "attributes": {"values": ["kept"]}}
    operation = writer.begin_run().submit(source)

    source["name"] = "caller-mutated"
    source["attributes"]["values"].append("caller-mutated")
    exposed = operation.event
    exposed["name"] = "operation-mutated"
    exposed["attributes"]["values"].append("operation-mutated")

    writer.flush()

    assert sent[0]["name"] == "original"
    assert sent[0]["attributes"] == {"values": ["kept"]}


def test_sender_mutation_cannot_change_retained_retry_payload():
    attempts = []
    fail = True

    def send(_run_id, events, _timeout):
        nonlocal fail
        attempts.append(json.loads(json.dumps(events)))
        events[0]["name"] = "sender-mutated"
        events[0]["nested"]["value"] = "sender-mutated"
        if fail:
            raise RuntimeError("offline")

    writer = BufferedObserveWriter(send, flush_records=10_000)
    operation = writer.begin_run().submit(
        {"name": "original", "nested": {"value": "kept"}}
    )

    with pytest.raises(ObserveDeliveryError):
        writer.flush()
    fail = False
    writer.flush()

    assert attempts == [
        [
            {
                "name": "original",
                "nested": {"value": "kept"},
                "event_id": operation.event_id,
            }
        ],
        [
            {
                "name": "original",
                "nested": {"value": "kept"},
                "event_id": operation.event_id,
            }
        ],
    ]


def test_buffered_observe_writer_flushes_completed_run():
    calls = []
    writer = BufferedObserveWriter(
        lambda run_id, events, _timeout: calls.append((run_id, events))
    )
    writer.begin_run().submit({"name": "terminal"}, completed=True)
    assert len(calls) == 1


def test_buffered_observe_writer_flushes_after_interval_without_new_submit():
    calls = []
    writer = BufferedObserveWriter(
        lambda run_id, events, _timeout: calls.append((run_id, events)),
        flush_interval=0.01,
    )
    run = writer.begin_run()
    run.submit({"name": "delayed"})
    deadline = time.monotonic() + 1
    while not calls and time.monotonic() < deadline:
        time.sleep(0.005)
    assert calls and calls[0][0] == run.run_id


def test_concurrent_close_callers_wait_for_and_share_delivery_failure():
    delivery_started = threading.Event()
    release_delivery = threading.Event()
    second_close_returned = threading.Event()
    errors = []

    def send(_run_id, _events, _timeout):
        delivery_started.set()
        assert release_delivery.wait(2)
        raise RuntimeError("offline")

    writer = BufferedObserveWriter(send, flush_records=10_000)
    writer.begin_run().submit({"name": "terminal"})

    def close(is_second=False):
        try:
            writer.close(timeout=1)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            if is_second:
                second_close_returned.set()

    first = threading.Thread(target=close)
    first.start()
    assert delivery_started.wait(1)
    second = threading.Thread(target=close, args=(True,))
    second.start()

    assert second_close_returned.wait(0.05) is False
    release_delivery.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(errors) == 2
    assert all(isinstance(error, ObserveDeliveryError) for error in errors)
    assert [str(error) for error in errors] == [
        "Observe write delivery failed",
        "Observe write delivery failed",
    ]


def test_invalid_close_timeout_leaves_queued_data_available_for_valid_close():
    sent = []
    writer = BufferedObserveWriter(
        lambda _run_id, events, _timeout: sent.extend(events),
        flush_records=100,
        flush_interval=60,
    )
    writer.begin_run().submit({"name": "queued"})

    with pytest.raises(ConfigurationError) as caught:
        writer.close(timeout=0)

    assert caught.value.reason == "invalid_timeout"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert sent == []

    writer.close()

    assert len(sent) == 1
    assert sent[0]["name"] == "queued"


@pytest.mark.parametrize("field", ["id", "event_id", "run_id"])
def test_buffered_writer_rejects_caller_owned_event_context(field):
    writer = BufferedObserveWriter(lambda *_args: None)
    run = writer.begin_run()

    with pytest.raises(ConfigurationError) as caught:
        run.submit({"name": "invalid", field: "caller-controlled"})

    assert caught.value.reason == "invalid_event"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    writer.close()


@pytest.mark.parametrize("field", sorted(FORBIDDEN_TELEMETRY_SELECTORS))
def test_buffered_writer_rejects_top_level_authority_and_unshipped_selectors(field):
    writer = BufferedObserveWriter(lambda *_args: None)
    run = writer.begin_run()

    with pytest.raises(ConfigurationError) as caught:
        run.submit({"name": "invalid", field: "server-owned"})

    assert caught.value.reason == "invalid_telemetry_scope"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    writer.close()


@pytest.mark.parametrize("completed", [1, "false", object()])
def test_buffered_writer_requires_boolean_completion(completed):
    writer = BufferedObserveWriter(lambda *_args: None)
    run = writer.begin_run()

    with pytest.raises(ConfigurationError) as caught:
        run.submit({"name": "invalid"}, completed=completed)

    assert caught.value.reason == "invalid_event"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    writer.close()


@pytest.mark.parametrize(
    "invalid_value",
    [
        {"nested": (1, 2)},
        {1: "non-string-key"},
        {"too_large": 1 << 63},
        {"not_finite": float("nan")},
    ],
)
def test_buffered_writer_rejects_noncanonical_json_before_queue_mutation(invalid_value):
    sent = []
    writer = BufferedObserveWriter(
        lambda _run_id, events, _timeout: sent.extend(events),
        flush_records=100,
        flush_interval=60,
    )
    run = writer.begin_run()

    with pytest.raises(ConfigurationError) as caught:
        run.submit({"name": "invalid", "attributes": invalid_value})

    assert caught.value.reason == "invalid_event"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    run.submit({"name": "valid", "attributes": {"kept": [1, 2]}})
    writer.close()
    assert [event["name"] for event in sent] == ["valid"]


def test_client_buffered_observe_sends_a_stable_run_with_each_event_batch():
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return _acknowledge(request)

    client = Client(
        base_url="https://test",
        api_key="ctk_test",
        agent_name="test-agent",
        transport=httpx.MockTransport(handle),
    )
    writer = client.buffered_observe(flush_records=10_000, max_request_records=2)
    run = writer.begin_run()
    run_id = run.run_id
    run.submit({"name": "first"})
    run.submit({"name": "second"})
    writer.flush()

    assert len(requests) == 2
    assert all(request["runs"][0]["id"] == run_id for request in requests)
    assert all(request["runs"][0]["status"] == "active" for request in requests)
    assert requests[0]["runs"][0]["started_at"] == requests[1]["runs"][0]["started_at"]


def test_client_buffered_observe_chunks_on_exact_serialized_wire_body(monkeypatch):
    requests = []
    run_id = "01890f3e-7b2a-7cc0-8f6a-123456789abc"
    event_id = "01890f3e-7b2a-7cc0-8f6a-123456789abd"
    one_event_body = {
        "runs": [
            {
                "id": run_id,
                "status": "active",
                "started_at": "2026-08-22T00:00:00.000000Z",
                "attributes_json": "{}",
            }
        ],
        "events": [{"name": "x", "id": event_id, "run_id": run_id}],
    }
    exact_one_event_bytes = len(
        json.dumps(
            one_event_body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    )

    def handle(request):
        requests.append(request.content)
        return _acknowledge(request)

    client = Client(
        base_url="https://test",
        api_key="ctk_test",
        agent_name="test-agent",
        transport=httpx.MockTransport(handle),
    )
    monkeypatch.setattr(telemetry, "uuid7", lambda: event_id)
    writer = client.buffered_observe(
        flush_records=10_000,
        max_request_bytes=exact_one_event_bytes,
    )
    run = writer.begin_run(run_id)
    run.submit({"name": "x"})
    run.submit({"name": "x"})

    writer.flush()

    assert [len(json.loads(body)["events"]) for body in requests] == [1, 1]
    assert all(len(body) <= exact_one_event_bytes for body in requests)


def test_client_buffered_observe_bounds_httpx_request_by_flush_deadline():
    request_timeouts = []

    def handle(request):
        request_timeouts.append(request.extensions["timeout"])
        return _acknowledge(request)

    client = Client(
        base_url="https://test",
        api_key="ctk_test",
        agent_name="test-agent",
        transport=httpx.MockTransport(handle),
    )
    writer = client.buffered_observe(flush_records=10_000)
    writer.begin_run().submit({"name": "bounded"})

    writer.flush(timeout=0.25)

    assert len(request_timeouts) == 1
    assert all(0 < value <= 0.25 for value in request_timeouts[0].values())


def test_client_buffered_observe_translates_event_id_to_strict_event_write_id():
    requests = []
    allowed_event_fields = {
        "id",
        "run_id",
        "parent_event_id",
        "decision_id",
        "name",
        "observation_kind",
        "operation_kind",
        "occurred_at",
        "latency_ms",
        "ttft_ms",
        "outcome",
        "input_json",
        "output_json",
        "error_json",
        "attributes_json",
        "provider",
        "model",
        "tool_id",
        "tool_version_id",
        "tool_name",
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    }

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        event = body["events"][0]
        unknown = set(event) - allowed_event_fields
        if unknown or not event.get("id"):
            return httpx.Response(400, json={"code": "invalid_argument"})
        return httpx.Response(
            200,
            json={
                "runs": [{"id": body["runs"][0]["id"], "replayed": False}],
                "events": [{"id": event["id"], "replayed": False}],
            },
        )

    client = Client(
        base_url="https://test",
        api_key="ctk_test",
        agent_name="test-agent",
        transport=httpx.MockTransport(handle),
    )
    writer = client.buffered_observe(flush_records=10_000)
    run = writer.begin_run()
    run_id = run.run_id
    operation = run.submit(
        {
            "name": "refund.create",
            "observation_kind": "tool_call",
            "operation_kind": "invoke",
            "occurred_at": "2026-08-22T12:00:00Z",
            "outcome": "success",
            "input_json": '{"amount":10}',
            "output_json": '"ok"',
            "attributes_json": "{}",
        }
    )
    writer.flush()

    assert requests[0]["events"][0]["id"] == operation.event_id
    assert "event_id" not in requests[0]["events"][0]
    assert requests[0]["events"][0]["run_id"] == run_id


def test_client_buffered_observe_completed_delivery_sends_terminal_run():
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return _acknowledge(request)

    client = Client(
        base_url="https://test",
        api_key="ctk_test",
        agent_name="test-agent",
        transport=httpx.MockTransport(handle),
    )
    writer = client.buffered_observe()
    writer.begin_run().submit({"name": "terminal"}, completed=True)

    run = requests[0]["runs"][0]
    assert run["status"] == "completed"
    assert run["ended_at"]


def test_client_buffered_observe_evicts_terminal_metadata_and_handles_are_single_use():
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return _acknowledge(request)

    client = Client(
        base_url="https://test",
        api_key="ctk_test",
        agent_name="test-agent",
        transport=httpx.MockTransport(handle),
    )
    writer = client.buffered_observe()
    runs = [writer.begin_run() for _ in range(100)]
    for run in runs:
        run.submit({"name": "terminal"}, completed=True)

    delivered = len(requests)
    with pytest.raises(ObserveRunCompletedError, match="Observe Run is completed"):
        runs[0].submit({"name": "late event"})
    assert len(requests) == delivered
    assert writer._active_runs == {}

    reused = writer.begin_run(runs[0].run_id)
    reused.submit({"name": "new lifecycle"}, completed=True)
    assert requests[-1]["runs"][0]["id"] == reused.run_id


def test_client_buffered_observe_retains_terminal_run_metadata_for_retry():
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(503, json={"code": "unavailable"})
        return _acknowledge(request)

    client = Client(
        base_url="https://test",
        api_key="ctk_test",
        agent_name="test-agent",
        transport=httpx.MockTransport(handle),
    )
    writer = client.buffered_observe()
    run = writer.begin_run()
    with pytest.raises(ObserveDeliveryError):
        run.submit({"name": "terminal"}, completed=True)
    writer.flush()

    assert requests[0] == requests[1]
    assert requests[1]["runs"][0]["status"] == "completed"
