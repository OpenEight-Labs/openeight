import threading
from openeight._delivery import DeliveryWorker
from openeight import DeliveryConfig, ConfigurationError


class Client:
    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()
        self.writes = []

    def write_telemetry(self, **body):
        self.entered.set()
        self.release.wait(2)
        self.writes.append(body)

    def register_tools(self, *args, **kwargs):
        raise ConfigurationError("invalid_tool_registration")


def test_capacity_includes_inflight_and_dependencies():
    client = Client()
    worker = DeliveryWorker(
        client, DeliveryConfig(max_records=2, flush_records=2), "complete", None
    )
    active = worker.enqueue("active", "run", {"id": "run", "status": "active"})
    assert client.entered.wait(1)
    complete = worker.enqueue(
        "complete", "run", {"id": "run", "status": "completed"}, (active,)
    )
    assert complete is not None
    assert worker.enqueue("event", "run", {}) is None
    assert worker.status().pending_records == 2
    assert worker.status().dropped_records == 1
    client.release.set()
    assert worker.flush(2).pending_records == 0
    assert [run["status"] for body in client.writes for run in body["runs"]] == [
        "active",
        "completed",
    ]
    worker.close(0.1)


def test_permanent_catalog_failure_is_independent_of_run():
    client = Client()
    client.release.set()
    worker = DeliveryWorker(client, DeliveryConfig(), "complete", None)
    # A nonempty job invokes the catalog sender; its rejection does not fail Runs.
    from openeight import ToolRegistration

    worker.enqueue(
        "catalog",
        "run",
        (
            (ToolRegistration(name="echo", description="", input_schema={}),),
            lambda result: None,
        ),
    )
    active = worker.enqueue("active", "run", {"id": "run"})
    worker.enqueue("complete", "run", {"id": "run", "status": "completed"}, (active,))
    status = worker.flush(2)
    assert status.failed_records == 1 and status.pending_records == 0
    assert any(
        run.get("status") == "completed"
        for body in client.writes
        for run in body["runs"]
    )
    worker.close(0.1)


def test_close_uses_one_deadline_and_retains_inflight_write():
    import time

    client = Client()
    worker = DeliveryWorker(client, DeliveryConfig(), "complete", None)
    worker.enqueue("active", "run", {"id": "run"})
    assert client.entered.wait(1)
    started = time.monotonic()
    try:
        status = worker.close(0.01)
        assert time.monotonic() - started < 0.1
        assert not status.closed and status.pending_records == 1
    finally:
        client.release.set()
    assert worker.close(2).closed
