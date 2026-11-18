"""Ordinary lifecycle uses real SDK/native execution with external HTTP doubled."""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from openeight import ConfigurationError, _adapter_http
from openeight._managed_types import ManagedResources
from openeight.integrations.starlette import lifespan
from openeight.langchain import create_agent

CONFIG = {
    "agent_name": "support",
    "endpoint": "https://control.example",
    "credential": "ctk_test",
}


@pytest.fixture
def requests(monkeypatch):
    requests = []

    class ExternalHTTP:
        def send(self, request, deadline):
            body = json.loads(request.content)
            requests.append((request.url.path, body))
            assert request.url.path.endswith("/WriteTelemetry")
            return httpx.Response(
                200,
                request=request,
                json={
                    field: [
                        {"id": item["id"], "replayed": False}
                        for item in body.get(field, [])
                    ]
                    for field in ("runs", "events")
                },
            )

        def close(self):
            pass

    monkeypatch.setattr(_adapter_http, "DeadlineHTTPTransport", ExternalHTTP)
    return requests


def agent(**kwargs):
    return create_agent(
        FakeMessagesListChatModel(responses=[AIMessage(content="done")]),
        openeight=CONFIG,
        **kwargs,
    )


def test_repeated_sync_transient_calls_and_terminal_close(requests):
    managed = agent()
    for _ in range(2):
        assert (
            managed.invoke({"messages": [("user", "hello")]})["messages"][-1].content
            == "done"
        )
        assert managed.lifecycle_status().state == "idle"
        assert managed.delivery_status().closed
    runs = {run["id"] for _, body in requests for run in body.get("runs", [])}
    assert len(runs) == 2
    managed.close()
    with pytest.raises(ConfigurationError) as caught:
        managed.invoke({"messages": []})
    assert caught.value.reason == "integration_closed"


def test_async_publication_has_no_run_or_model_call(requests):
    async def run():
        managed = agent()
        snapshot = await managed.apublish()
        assert snapshot.publication_complete
        assert not requests
        assert managed.lifecycle_status().state == "idle"
        assert managed.validate().supported is True
        await managed.aclose()

    asyncio.run(run())


def test_persistent_async_session_and_idle_stream_rejection(requests):
    async def run():
        managed = agent()
        with pytest.raises(ConfigurationError) as caught:
            await anext(managed.astream({"messages": []}))
        assert caught.value.reason == "integration_not_entered"
        assert not requests
        async with managed:
            assert await managed.aopen() is managed
            with pytest.raises(ConfigurationError):
                await managed.__aenter__()
            values = await managed.abatch(
                [{"messages": [("user", "one")]}, {"messages": [("user", "two")]}]
            )
            assert len(values) == 2
        assert managed.lifecycle_status().state == "closed"
        assert managed.lifecycle_status().effects_settled

    asyncio.run(run())


def test_old_constructor_keyword_is_not_accepted():
    with pytest.raises(TypeError):
        create_agent(FakeMessagesListChatModel(responses=[]), governance=CONFIG)


def test_lifespan_preserves_state_dependency_order_and_borrowed_sessions(requests):
    order = []
    state = {"application": object()}

    @asynccontextmanager
    async def existing(app):
        order.append("app-open")
        try:
            yield state
        finally:
            order.append("app-close")

    def resources(label):
        @asynccontextmanager
        async def resource():
            order.append(label + "-open")
            try:
                yield ManagedResources()
            finally:
                order.append(label + "-close")

        return resource

    async def run():
        first, second = (
            agent(resources=resources("first")),
            agent(resources=resources("second")),
        )
        async with lifespan(first, second, existing=existing)(object()) as actual:
            assert actual is state
            assert (
                first.lifecycle_status().state
                == second.lifecycle_status().state
                == "open"
            )
        assert order == [
            "app-open",
            "first-open",
            "second-open",
            "second-close",
            "first-close",
            "app-close",
        ]
        borrowed = agent()
        await borrowed.aopen()
        with pytest.raises(ConfigurationError):
            async with lifespan(borrowed)(object()):
                pytest.fail("borrowed owner accepted")
        assert borrowed.lifecycle_status().state == "open"
        await borrowed.aclose()

    asyncio.run(run())


def test_terminal_exit_runs_once_and_keeps_primary_error(requests):
    from openeight import ResourceCleanupError

    exits = []

    @asynccontextmanager
    async def resources():
        try:
            yield ManagedResources()
        finally:
            exits.append(True)
            raise RuntimeError("private-resource-secret")

    async def run():
        managed = agent(resources=resources)
        with pytest.raises(ResourceCleanupError) as caught:
            async with managed:
                pass
        assert "private-resource-secret" not in str(caught.value)
        assert managed.lifecycle_status().cleanup_terminal
        with pytest.raises(ResourceCleanupError):
            await managed.aclose()
        assert len(exits) == 1
        primary = ValueError("application-error")
        second = agent(resources=resources)
        with pytest.raises(ValueError) as caught:
            async with second:
                raise primary
        assert caught.value is primary
        assert len(exits) == 2

    asyncio.run(run())


def test_overlapping_async_batches_keep_operation_outcomes_isolated(requests):
    async def run():
        managed = create_agent(
            FakeMessagesListChatModel(
                responses=[AIMessage(content="done")], sleep=0.05
            ),
            openeight=CONFIG,
        )
        async with managed:
            first = asyncio.create_task(
                managed.abatch(
                    [{"messages": [("user", "one")]}, {"messages": [("user", "two")]}]
                )
            )
            await asyncio.sleep(0.01)
            second = asyncio.create_task(
                managed.abatch([{"messages": [("user", "three")]}])
            )
            results = await asyncio.gather(first, second)
            assert list(map(len, results)) == [2, 1]

    asyncio.run(run())


def test_close_during_transient_startup_cannot_reopen_closed_agent(requests):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        @asynccontextmanager
        async def resources():
            entered.set()
            await release.wait()
            yield ManagedResources()

        managed = agent(resources=resources)
        publishing = asyncio.create_task(managed.apublish())
        await entered.wait()
        with pytest.raises(ConfigurationError) as caught:
            await managed.aclose(0.01)
        assert caught.value.reason == "integration_busy"
        release.set()
        await publishing
        await managed.aclose()
        with pytest.raises(ConfigurationError) as caught:
            await managed.aopen()
        assert caught.value.reason == "integration_closed"

    asyncio.run(run())


def test_local_report_rejects_unknown_saver_without_opening_it(requests):
    managed = agent(checkpointer=object())
    report = managed.validate()
    assert report.supported is False
    assert any(issue.code == "unsupported_checkpointer" for issue in report.issues)
    assert not requests


def test_cancellation_during_successful_context_cleanup_retains_cancellation(requests):
    async def run():
        exiting, release = asyncio.Event(), asyncio.Event()

        @asynccontextmanager
        async def resources():
            try:
                yield ManagedResources()
            finally:
                exiting.set()
                await release.wait()

        managed = agent(resources=resources)

        async def context():
            async with managed:
                pass

        task = asyncio.create_task(context())
        await exiting.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        status = managed.lifecycle_status()
        assert status.cleanup_complete and status.effects_settled
        assert status.error_code is None

    asyncio.run(run())


def test_incomplete_explicit_close_during_transient_execution_requires_acknowledgment(
    requests,
):
    from openeight import CleanupIncompleteError

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        class BlockingProvider(FakeMessagesListChatModel):
            async def _agenerate(self, *args, **kwargs):
                entered.set()
                await release.wait()
                return await super()._agenerate(*args, **kwargs)

        managed = create_agent(
            BlockingProvider(responses=[AIMessage(content="done")]), openeight=CONFIG
        )
        executing = asyncio.create_task(
            managed.ainvoke({"messages": [("user", "hello")]})
        )
        await entered.wait()
        with pytest.raises(CleanupIncompleteError):
            await managed.aclose(0.001)
        release.set()
        await executing
        assert managed.lifecycle_status().state == "settling"
        assert managed.lifecycle_status().effects_settled
        with pytest.raises(ConfigurationError) as caught:
            await managed.ainvoke({"messages": [("user", "again")]})
        assert caught.value.reason == "integration_busy"
        await managed.aclose()
        assert managed.lifecycle_status().state == "closed"

    asyncio.run(run())


def test_abandoned_stream_reports_owned_run_until_explicit_iterator_close(requests):
    from openeight import CleanupIncompleteError

    async def run():
        managed = agent()
        async with managed:
            iterator = managed.astream({"messages": [("user", "hello")]})
            await anext(iterator)
            assert managed.lifecycle_status().active_executions == 1
            with pytest.raises(CleanupIncompleteError) as caught:
                await managed.aclose(0.001)
            assert len(caught.value.operation.run_ids) == 1
            assert not managed.lifecycle_status().effects_settled
            await iterator.aclose()
            await managed.aclose()
            assert managed.lifecycle_status().cleanup_complete

    asyncio.run(run())


def test_context_exit_keeps_dependencies_past_close_deadline_until_effect_settles(
    monkeypatch,
):
    from _external_control import ExternalControlPlane
    from langchain_core.tools import tool
    from test_framework_boundary import ToolModel

    service = ExternalControlPlane()
    monkeypatch.setattr(_adapter_http, "DeadlineHTTPTransport", lambda: service)

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        order = []

        @asynccontextmanager
        async def resources():
            try:
                yield ManagedResources()
            finally:
                order.append("dependencies-exited")

        @tool
        async def write_todos(value: str) -> str:
            """Finish an external effect before closing its dependency."""
            entered.set()
            await release.wait()
            order.append("effect-settled")
            return value

        managed = create_agent(
            ToolModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "effect",
                                "name": "write_todos",
                                "args": {"value": "once"},
                            }
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            ),
            [write_todos],
            openeight=CONFIG,
            resources=resources,
        )

        async def context():
            async with managed:
                await managed.ainvoke({"messages": [("user", "go")]})

        task = asyncio.create_task(context())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(10.05)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert order == []
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert order == ["effect-settled", "dependencies-exited"]
        assert managed.lifecycle_status().cleanup_complete

    asyncio.run(run())
