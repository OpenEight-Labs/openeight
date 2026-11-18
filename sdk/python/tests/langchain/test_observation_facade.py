import asyncio
import json
import threading

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from test_framework_boundary import fixture

from openeight import ConfigurationError
from _binding_helpers import instrument
from openeight.langchain import _owner as implementation
from openeight.langchain._runtime import CURRENT_INVOCATION


class RecordingClient:
    def __init__(self, *args, **kwargs):
        self.writes, self.registrations = [], []
        self.threads = []
        self.fail_catalog = False

    def get_tool_readiness(self, ids, **kwargs):
        from openeight.tools import AgentReadiness

        return AgentReadiness((), True, "ready", True, "ready")

    def register_tools(self, facts, **kwargs):
        self.registrations.append(facts)
        self.threads.append(threading.current_thread().name)
        if self.fail_catalog:
            raise ConfigurationError("invalid_tool_registration")
        return ()

    def write_telemetry(self, **kwargs):
        self.writes.append(kwargs)
        self.threads.append(threading.current_thread().name)

    def close(self):
        pass


@pytest.fixture
def bind(monkeypatch):
    monkeypatch.setattr(implementation, "Client", RecordingClient)
    owners = []

    def bind(graph):
        owner = instrument(
            graph,
            agent_name="test",
            endpoint="http://localhost:7777",
            credential="ctk_test",
        )
        owners.append(owner)
        return owner

    yield bind
    for owner in owners:
        owner.close(0.1)


def test_existing_graph_sync_and_batch_parity(bind):
    graph, effects, order = fixture()
    owner = bind(graph)
    assert owner.catalog.completeness == "complete"
    config = {"tags": ["user"], "metadata": {"secret": "private"}, "callbacks": []}
    result = owner.invoke({"messages": [("user", "private")]}, config)
    assert result["messages"][-1].content == "done"
    assert effects == ["changed", "changed"]
    assert config == {
        "tags": ["user"],
        "metadata": {"secret": "private"},
        "callbacks": [],
    }
    status = owner.flush(2)
    assert status.pending_records == 0
    assert {fact.name for fact in owner.client.registrations[0]} == {"echo", "unused"}
    runs = [run for write in owner.client.writes for run in write.get("runs", [])]
    assert [run["status"] for run in runs] == ["active", "completed"]
    assert all(run["attributes_json"] == "{}" for run in runs)
    events = [
        event for write in owner.client.writes for event in write.get("events", [])
    ]
    assert (
        len([event for event in events if event["observation_kind"] == "tool_call"])
        == 2
    )
    assert all(event["name"] and len(event["name"].encode()) <= 128 for event in events)
    assert {
        event["name"] for event in events if event["observation_kind"] == "tool_call"
    } == {"echo"}
    assert any(event.get("span_role") == "graph" for event in events)
    assert all("input_json" not in event and "output_json" not in event for event in events)
    assert all("private" not in event["attributes_json"] for event in events)
    assert "private" not in repr(owner.delivery_status())
    assert set(owner.client.threads) == {"openeight-delivery"}
    plain = bind(
        create_agent(FakeMessagesListChatModel(responses=[AIMessage(content="done")]))
    )
    assert (
        len(
            plain.batch(
                [{"messages": [("user", "x")]}, {"messages": [("user", "y")]}],
                {"max_concurrency": 2},
            )
        )
        == 2
    )


@pytest.mark.parametrize("startup_pending", [True, False])
def test_catalog_failure_does_not_block_completion(bind, monkeypatch, startup_pending):
    entered, release = threading.Event(), threading.Event()

    def fail(self, facts, **kwargs):
        self.registrations.append(facts)
        entered.set()
        assert release.wait(2)
        raise ConfigurationError("invalid_tool_registration")

    monkeypatch.setattr(RecordingClient, "register_tools", fail)
    graph, _, _ = fixture()
    owner = bind(graph)
    try:
        assert entered.wait(2)
        if not startup_pending:
            release.set()
            assert owner.flush(2).failed_records == 1
        owner.invoke({"messages": [("user", "x")]})
    finally:
        release.set()
    status = owner.flush(2)
    assert status.failed_records == (1 if startup_pending else 2)
    assert status.catalog_failed_tools == 2
    assert any(
        run["status"] == "completed"
        for write in owner.client.writes
        for run in write.get("runs", [])
    )


def test_sync_stream_lazy_admission_and_context_reset(bind):
    graph, _, _ = fixture()
    owner = bind(graph)
    iterator = owner.stream({"messages": [("user", "x")]}, stream_mode="updates")
    assert not owner.roots and not owner.client.writes
    next(iterator)
    assert CURRENT_INVOCATION.get() is None
    iterator.close()
    assert owner.flush(2).pending_records == 0


def test_async_methods_and_stream_modes(bind):
    asyncio.run(_async_methods(bind))


async def _async_methods(bind):
    graph, _, _ = fixture()
    owner = bind(graph)
    assert (await owner.ainvoke({"messages": [("user", "x")]}))["messages"][
        -1
    ].content == "done"
    graph = create_agent(
        FakeMessagesListChatModel(responses=[AIMessage(content="done")])
    )
    plain = bind(graph)
    assert (
        len(
            await plain.abatch(
                [{"messages": [("user", "x")]}, {"messages": [("user", "y")]}]
            )
        )
        == 2
    )
    for mode in ("values", "updates", "messages"):
        graph, _, _ = fixture()
        streamed = bind(graph)
        chunks = []
        async for chunk in streamed.astream(
            {"messages": [("user", "x")]}, stream_mode=mode
        ):
            assert CURRENT_INVOCATION.get() is None
            chunks.append(chunk)
        assert chunks
        assert (await streamed.aflush(2)).pending_records == 0


def test_unsupported_and_closed_execution(bind):
    from langgraph.types import Command

    graph, _, _ = fixture()
    owner = bind(graph)
    with pytest.raises(ConfigurationError):
        owner.invoke(Command(resume=True))
    with pytest.raises(ConfigurationError):
        next(owner.transform(iter([])))
    owner.close(0.1)
    with pytest.raises(ConfigurationError):
        owner.invoke({})


def test_expired_executor_queue_deadline_never_starts_http(bind):
    import time

    from openeight import TransportError

    owner = bind(fixture()[0])
    release = threading.Event()
    started = []
    futures = [owner.executor.submit(release.wait, 2) for _ in range(4)]

    async def check():
        task = asyncio.create_task(
            owner._run_client_call(
                lambda: started.append(True), deadline=time.monotonic() + 0.01
            )
        )
        await asyncio.sleep(0.02)
        release.set()
        with pytest.raises(TransportError):
            await task

    asyncio.run(check())
    assert not started
    for future in futures:
        future.result(2)


def test_queued_close_obeys_deadline_and_seals_admission(bind):
    import time

    owner = bind(fixture()[0])
    release = threading.Event()
    futures = [owner.executor.submit(release.wait, 2) for _ in range(4)]

    async def check():
        started = time.monotonic()
        status = await owner.aclose(0.02)
        assert time.monotonic() - started < 0.2
        assert not status.closed and status.last_error_code == "flush_timeout"
        with pytest.raises(ConfigurationError):
            owner.invoke({"messages": [("user", "x")]})
        release.set()

    try:
        asyncio.run(check())
    finally:
        release.set()
        for future in futures:
            future.result(2)
    assert owner.close(2).closed


def test_async_observation_admission_does_not_depend_on_http_executor(bind):
    owner = bind(
        create_agent(FakeMessagesListChatModel(responses=[AIMessage(content="done")]))
    )
    for _ in range(1000):
        assert owner.executor_slots.acquire(blocking=False)
    try:
        result = asyncio.run(owner.ainvoke({"messages": [("user", "x")]}))
        assert result["messages"][-1].content == "done"
    finally:
        for _ in range(1000):
            owner.executor_slots.release()
    assert owner.flush(2).pending_records == 0


@pytest.mark.parametrize("method", ["aflush", "aclose"])
def test_async_shutdown_reports_exhausted_executor_capacity(bind, method):
    owner = bind(fixture()[0])
    for _ in range(1000):
        assert owner.executor_slots.acquire(blocking=False)
    try:
        status = asyncio.run(getattr(owner, method)(0.01))
        assert not status.closed and status.last_error_code == "execution_busy"
        if method == "aclose":
            with pytest.raises(ConfigurationError):
                owner.invoke({})
    finally:
        for _ in range(1000):
            owner.executor_slots.release()
    assert owner.close(2).closed


@pytest.mark.parametrize("method", ["aflush", "aclose"])
def test_async_shutdown_preserves_error_when_delivery_updates_status(
    bind, monkeypatch, method
):
    owner = bind(fixture()[0])
    owner.flush(2)
    original_status = owner.delivery_status

    def concurrent_delivery_status():
        with owner.worker.condition:
            owner.worker.last_error = "catalog_failed"
        return original_status()

    monkeypatch.setattr(owner, "delivery_status", concurrent_delivery_status)
    for _ in range(1000):
        assert owner.executor_slots.acquire(blocking=False)
    try:
        status = asyncio.run(getattr(owner, method)(0.01))
        assert not status.closed and status.last_error_code == "execution_busy"
        assert owner.worker.last_error == "catalog_failed"
    finally:
        for _ in range(1000):
            owner.executor_slots.release()


def test_nested_facade_has_only_local_event_parents(bind):
    from langchain_core.runnables import RunnableConfig
    from langchain_core.tools import tool
    from test_framework_boundary import ToolModel

    inner = bind(
        create_agent(FakeMessagesListChatModel(responses=[AIMessage(content="inner")]))
    )
    configs = []
    from langchain_core.callbacks import BaseCallbackHandler

    class UserCallbacks(BaseCallbackHandler):
        def __init__(self):
            self.parents = []

        def on_chain_start(
            self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs
        ):
            self.parents.append(parent_run_id)

    user_callbacks = UserCallbacks()

    @tool
    def nested(value: str, config: RunnableConfig) -> str:
        """Invoke another observed agent."""
        parent = config["callbacks"].parent_run_id
        configs.append(parent)
        result = inner.invoke({"messages": [("user", value)]}, config)
        assert config["callbacks"].parent_run_id == parent
        return result["messages"][-1].content

    outer = bind(
        create_agent(
            ToolModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "nested-call",
                                "name": "nested",
                                "args": {"value": "hello"},
                            }
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            ),
            tools=[nested],
        )
    )
    assert (
        outer.invoke({"messages": [("user", "x")]}, {"callbacks": [user_callbacks]})[
            "messages"
        ][-1].content
        == "done"
    )
    assert configs[0] is not None
    assert user_callbacks.parents.count(None) == 1
    for owner in (inner, outer):
        assert owner.flush(2).pending_records == 0
        events = [
            event for write in owner.client.writes for event in write.get("events", [])
        ]
        ids = {event["id"] for event in events}
        assert events
        assert all(
            not event.get("parent_event_id") or event["parent_event_id"] in ids
            for event in events
        )


@pytest.mark.parametrize("method", ["invoke", "ainvoke", "stream", "astream"])
def test_config_failure_releases_admitted_invocation(bind, method):
    owner = bind(fixture()[0])

    async def run_async():
        if method == "ainvoke":
            await owner.ainvoke({}, {"callbacks": object()})
        else:
            async for _ in owner.astream({}, {"callbacks": object()}):
                pass

    with pytest.raises(AttributeError):
        if method in ("ainvoke", "astream"):
            asyncio.run(run_async())
        elif method == "stream":
            next(owner.stream({}, {"callbacks": object()}))
        else:
            owner.invoke({}, {"callbacks": object()})
    assert owner.close(2).closed
    assert not owner.roots


def test_instrument_reports_catalog_before_any_invocation(bind):
    graph, effects, _ = fixture()
    owner = bind(graph)
    owner.flush(2)
    assert len(owner.client.registrations) == 1
    assert {fact.name for fact in owner.client.registrations[0]} == {"echo", "unused"}
    assert effects == [] and owner.client.writes == [] and not owner.roots


def test_startup_publication_failure_remains_pending_until_close(monkeypatch):
    from openeight import TransportError

    closed = []

    class FailingClient(RecordingClient):
        def register_tools(self, *args, **kwargs):
            raise TransportError()

        def close(self):
            closed.append(True)

    monkeypatch.setattr(implementation, "Client", FailingClient)
    graph, effects, _ = fixture()
    owner = instrument(
        graph,
        agent_name="test",
        endpoint="http://localhost:7777",
        credential="ctk_test",
    )
    try:
        assert owner.flush(0.05).catalog_pending_tools == 2
        assert closed == [] and effects == []
        owner.close(0.05)
        assert owner.close(2).closed
        assert owner.delivery_status().catalog_dropped_tools == 2
    finally:
        owner.close(2)
    assert closed == [True] and effects == []
