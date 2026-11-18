"""Sync callers share the async gate, invocation ownership and cleanup."""

import asyncio
import threading
from contextlib import asynccontextmanager
from contextvars import ContextVar

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from test_enforcement_facade import GovernedClient
from test_framework_boundary import ToolModel
from test_managed_langchain import config

from openeight import CleanupIncompleteError, ConfigurationError
from openeight.langchain import _owner, create_agent
from openeight.managed import ManagedResources

INPUT = {"messages": [("user", "go")]}


@pytest.mark.parametrize("observed", [False, True])
def test_ordinary_facade_does_not_expose_durable_deployment_helpers(
    monkeypatch, observed
):
    from openeight.langchain import observe

    client = GovernedClient()
    monkeypatch.setattr(_owner, "Client", lambda *a, **k: client)
    settings = config()
    model = ToolModel(responses=[AIMessage(content="done")])
    agent = (
        observe(
            model,
            openeight={
                "agent_name": settings.agent_name,
                "endpoint": settings.endpoint,
                "credential": settings.credential,
            },
        )
        if observed
        else create_agent(model, openeight=settings)
    )
    with agent:
        assert not hasattr(agent, "bind_deployment")
        assert not hasattr(agent, "validate_deployment")
    assert client.requests == []


def make(monkeypatch, *, tools=(), resources=None, calls=()):
    client = GovernedClient()
    monkeypatch.setattr(_owner, "Client", lambda *a, **k: client)
    responses = [*calls, AIMessage(content="done")]
    return create_agent(
        ToolModel(responses=responses), tools, openeight=config(), resources=resources
    ), client


def test_sync_invoke_uses_async_governance_and_contextvars(monkeypatch):
    seen = []
    value = ContextVar("sync-test", default=None)

    @tool
    async def effect(text: str) -> str:
        """Record a governed effect."""
        seen.append((text, value.get(), threading.get_ident()))
        return text

    call = AIMessage(
        content="",
        tool_calls=[dict(name="effect", args={"text": "ok"}, id="native-id")],
    )
    owner, client = make(monkeypatch, tools=[effect], calls=[call])
    with owner as agent:
        token = value.set("submitted")
        try:
            assert agent.invoke(INPUT)["messages"][-1].content == "done"
        finally:
            value.reset(token)
        assert len(client.requests) == 1
        assert seen == [("ok", "submitted", agent._session._bridge.thread.ident)]
        assert agent._session._bridge.thread.ident != threading.get_ident()
    assert owner.delivery_status().closed
    assert not owner._session._bridge.thread.is_alive()


@pytest.mark.parametrize("mode", ["values", "updates", "messages"])
def test_sync_stream_is_lazy_ordered_and_holds_owner_until_close(monkeypatch, mode):
    owner, client = make(monkeypatch)
    with owner as agent:
        stream = agent.stream(INPUT, stream_mode=mode)
        assert not owner._session._owner.roots
        first = next(stream)
        assert first is not None
        with pytest.raises(ConfigurationError) as error:
            agent.invoke(INPUT)
        assert error.value.reason == "integration_busy"
        stream.close()
        with pytest.raises(StopIteration):
            next(stream)
        assert agent.invoke(INPUT)["messages"][-1].content == "done"


def test_sync_batch_preserves_order_and_async_configuration(monkeypatch):
    owner, _ = make(monkeypatch)
    with owner as agent:
        results = agent.batch([INPUT, INPUT], {"max_concurrency": 1})
        assert [r["messages"][-1].content for r in results] == ["done", "done"]
        assert agent.batch([]) == []


def test_mode_mixing_and_loop_reentry_reject_before_dispatch(monkeypatch):
    owner, _ = make(monkeypatch)
    with owner as agent:

        async def misuse():
            with pytest.raises(ConfigurationError) as error:
                await agent.ainvoke(INPUT)
            assert error.value.reason == "integration_mode_mismatch"
            with pytest.raises(ConfigurationError) as error:
                agent.invoke(INPUT)
            assert error.value.reason == "integration_sync_reentry"

        asyncio.run(misuse())
        with pytest.raises(ConfigurationError):
            agent.__enter__()
        assert not agent._session._owner.roots

    async def async_mode():
        other, _ = make(monkeypatch)
        async with other:
            with pytest.raises(ConfigurationError) as error:
                other.invoke(INPUT)
            assert error.value.reason == "integration_mode_mismatch"

    asyncio.run(async_mode())


def test_resources_enter_and_exit_once_on_owner_loop(monkeypatch):
    lifecycle = []

    @asynccontextmanager
    async def resources():
        lifecycle.append(("enter", threading.get_ident(), asyncio.get_running_loop()))
        yield ManagedResources()
        lifecycle.append(("exit", threading.get_ident(), asyncio.get_running_loop()))

    owner, _ = make(monkeypatch, resources=resources)
    with owner as agent:
        agent.invoke(INPUT)
        assert agent.close().closed
        assert agent.close().closed
    assert [x[0] for x in lifecycle] == ["enter", "exit"]
    assert lifecycle[0][1:] == lifecycle[1][1:]
    assert lifecycle[0][1] == owner._session._bridge.thread.ident


def test_cleanup_failure_is_terminal_and_thread_stops(monkeypatch):
    exits = []

    @asynccontextmanager
    async def resources():
        try:
            yield ManagedResources()
        finally:
            exits.append(True)
            raise RuntimeError("private-resource-detail")

    owner, _ = make(monkeypatch, resources=resources)
    with pytest.raises(ConfigurationError) as error:
        with owner:
            owner.invoke(INPUT)
    assert error.value.reason == "integration_resource_cleanup_failed"
    assert "private-resource-detail" not in str(error.value)
    assert exits == [True]
    assert not owner._session._bridge.thread.is_alive()
    assert not owner.delivery_status().closed
    assert (
        owner.delivery_status().last_error_code == "integration_resource_cleanup_failed"
    )
    with pytest.raises(ConfigurationError):
        owner.close()
    assert exits == [True]


def test_resource_conflict_rejects_at_construction(monkeypatch):
    with pytest.raises(ConfigurationError) as error:
        create_agent(
            ToolModel(responses=[AIMessage(content="done")]),
            openeight=config(),
            resources=lambda: None,
            store=object(),
        )
    assert error.value.reason == "integration_resource_conflict"


def test_busy_sync_owner_allows_status_and_bounded_close(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    effects = []

    @tool
    def effect() -> str:
        """Wait until explicitly released."""
        entered.set()
        assert release.wait(5)
        effects.append("once")
        return "done"

    call = AIMessage(content="", tool_calls=[dict(name="effect", args={}, id="effect")])
    owner, _ = make(monkeypatch, tools=[effect], calls=[call])
    errors = []

    def execute():
        try:
            owner.invoke(INPUT)
        except BaseException as error:
            errors.append(error)

    with owner:
        caller = threading.Thread(target=execute)
        caller.start()
        assert entered.wait(5)
        try:
            with pytest.raises(ConfigurationError) as error:
                owner.invoke(INPUT)
            assert error.value.reason == "integration_busy"
            assert not owner.delivery_status().closed
            with pytest.raises(CleanupIncompleteError):
                owner.close(timeout=0.01)
            assert not owner.delivery_status().closed
            assert owner._session._bridge.thread.is_alive()
        finally:
            release.set()
            caller.join(5)
        assert not caller.is_alive()
    assert effects == ["once"]
    assert not errors


def test_cancel_before_loop_submission_releases_execution_slot(monkeypatch):
    owner, _ = make(monkeypatch)
    with owner:
        blocked, release = threading.Event(), threading.Event()

        def block():
            blocked.set()
            assert release.wait(5)

        owner._session._bridge.loop.call_soon_threadsafe(block)
        assert blocked.wait(5)
        assert owner._session._bridge.busy.acquire(blocking=False)

        async def never():
            pytest.fail("cancelled submission started")

        future = owner._session._bridge._submit(never, release=True)
        assert future.cancel()
        release.set()
        owner.catalog_status()  # barrier through the same loop
        assert not owner._session._bridge.busy.locked()
        assert owner.invoke(INPUT)["messages"][-1].content == "done"


def test_cancelled_resource_exit_has_stable_terminal_failure(monkeypatch):
    exits = []

    @asynccontextmanager
    async def resources():
        try:
            yield ManagedResources()
        finally:
            exits.append(True)
            raise asyncio.CancelledError()

    owner, _ = make(monkeypatch, resources=resources)
    with pytest.raises(ConfigurationError) as error:
        with owner:
            owner.invoke(INPUT)
            owner.close()
    assert error.value.reason == "integration_resource_cleanup_failed"
    assert exits == [True]
    assert not owner._session._bridge.thread.is_alive()
    assert (
        owner.delivery_status().last_error_code == "integration_resource_cleanup_failed"
    )
    with pytest.raises(ConfigurationError):
        owner.close()
    assert exits == [True]


def test_stream_close_retains_slot_until_admitted_effect_settles(monkeypatch):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    @tool
    async def effect() -> str:
        """Wait for release without blocking the owner loop."""
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.001)
        return "done"

    call = AIMessage(content="", tool_calls=[dict(name="effect", args={}, id="effect")])
    owner, _ = make(monkeypatch, tools=[effect], calls=[call])
    errors = []
    with owner:
        stream = owner.stream(INPUT, stream_mode="updates")

        def consume():
            try:
                list(stream)
            except BaseException as error:
                errors.append(error)

        caller = threading.Thread(target=consume)
        caller.start()
        assert entered.wait(5)

        def close_stream():
            stream.close()
            closed.set()

        closer = threading.Thread(target=close_stream)
        closer.start()
        try:
            assert not closed.wait(0.05)
            with pytest.raises(ConfigurationError) as error:
                owner.invoke(INPUT)
            assert error.value.reason == "integration_busy"
            assert owner._session._bridge.busy.locked()
        finally:
            release.set()
            caller.join(5)
            closer.join(5)
        assert not caller.is_alive() and not closer.is_alive()
        assert closed.is_set()
        assert not owner._session._bridge.busy.locked()


def test_sync_tool_cannot_reenter_owner_lifecycle(monkeypatch):
    holder = {}
    errors = []

    @tool
    def effect() -> str:
        """Check callback re-entry without waiting on our own execution."""
        agent = holder["agent"]
        for operation in (
            agent.close,
            lambda: agent.__exit__(None, None, None),
            lambda: agent.invoke(INPUT),
        ):
            try:
                operation()
            except ConfigurationError as error:
                errors.append(error.reason)
        return "done"

    call = AIMessage(content="", tool_calls=[dict(name="effect", args={}, id="effect")])
    owner, _ = make(monkeypatch, tools=[effect], calls=[call])
    with owner:
        holder["agent"] = owner
        owner.invoke(INPUT)
    assert errors == ["integration_sync_reentry"] * 3


def test_interrupted_stream_close_keeps_slot_until_handler_settles(monkeypatch):
    from concurrent.futures import Future

    entered, release, interrupted = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    @tool
    async def effect() -> str:
        """An admitted effect outlives a caller interruption."""
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.001)
        return "done"

    call = AIMessage(content="", tool_calls=[dict(name="effect", args={}, id="effect")])
    owner, _ = make(monkeypatch, tools=[effect], calls=[call])
    original = Future.result

    def result(self, *args, **kwargs):
        if threading.current_thread().name == "interrupt-close":
            raise KeyboardInterrupt
        return original(self, *args, **kwargs)

    errors = []
    with owner:
        stream = owner.stream(INPUT, stream_mode="updates")

        def consume():
            try:
                list(stream)
            except BaseException as error:
                errors.append(type(error))

        caller = threading.Thread(target=consume)
        caller.start()
        assert entered.wait(5)
        monkeypatch.setattr(Future, "result", result)

        def close_stream():
            try:
                stream.close()
            except KeyboardInterrupt:
                interrupted.set()

        closer = threading.Thread(target=close_stream, name="interrupt-close")
        closer.start()
        assert interrupted.wait(5)
        try:
            assert owner._session._bridge.busy.locked()
            with pytest.raises(ConfigurationError) as error:
                owner.invoke(INPUT)
            assert error.value.reason == "integration_busy"
        finally:
            release.set()
            caller.join(5)
            closer.join(5)
        stream.close()
        assert not owner._session._bridge.busy.locked()


def test_interrupted_context_exit_drains_admitted_handler_before_stopping_loop(
    monkeypatch,
):
    from concurrent.futures import Future

    entered, release, attempted = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    settled, cancelled, interrupted = [], [], []

    @tool
    async def effect() -> str:
        """An admitted effect must survive interruption of context exit."""
        entered.set()
        try:
            while not release.is_set():
                await asyncio.sleep(0.001)
            settled.append(True)
            return "done"
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    call = AIMessage(content="", tool_calls=[dict(name="effect", args={}, id="effect")])
    owner, _ = make(monkeypatch, tools=[effect], calls=[call])
    owner.__enter__()
    stream = owner.stream(INPUT, stream_mode="updates")

    def consume():
        try:
            list(stream)
        except BaseException:
            pass

    caller = threading.Thread(target=consume)
    caller.start()
    assert entered.wait(5)
    original = Future.result

    def result(self, *args, **kwargs):
        if threading.current_thread().name == "interrupt-exit":
            attempted.set()
            raise KeyboardInterrupt
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Future, "result", result)

    def exit_owner():
        try:
            owner.__exit__(None, None, None)
        except KeyboardInterrupt:
            interrupted.append(True)

    closer = threading.Thread(target=exit_owner, name="interrupt-exit")
    closer.start()
    try:
        assert attempted.wait(5)
        assert closer.is_alive()
        assert owner._session._bridge.thread.is_alive()
        assert not settled and not cancelled
    finally:
        release.set()
        caller.join(5)
        closer.join(5)
    assert settled == [True] and not cancelled and interrupted == [True]
    assert not owner._session._bridge.thread.is_alive()


@pytest.mark.parametrize("integration", ["langchain", "deepagents"])
def test_both_frameworks_sync_resource_factory_checkpoint_lifecycle(
    monkeypatch, integration, tmp_path
):
    saver_module = pytest.importorskip("langgraph.checkpoint.sqlite.aio")
    AsyncSqliteSaver = saver_module.AsyncSqliteSaver
    if integration == "deepagents":
        pytest.importorskip("deepagents")
        from openeight.deepagents import create_deep_agent

    client = GovernedClient()
    monkeypatch.setattr(_owner, "Client", lambda *a, **k: client)
    loops = []

    @asynccontextmanager
    async def resources():
        async with AsyncSqliteSaver.from_conn_string(
            str(tmp_path / "checkpoint.sqlite")
        ) as saver:
            loops.append(asyncio.get_running_loop())
            yield ManagedResources(checkpointer=saver)

    create = create_agent if integration == "langchain" else create_deep_agent
    owner = create(
        ToolModel(responses=[AIMessage(content="done")]),
        openeight=config(),
        resources=resources,
    )
    with owner:
        thread = owner.new_thread()
        result = owner.invoke(INPUT, {"configurable": {"thread_id": thread}})
        assert result["messages"][-1].content == "done"
        assert loops == [owner._session._loop]
        owner.release_thread(thread)
    assert not owner._session._bridge.thread.is_alive()


def test_repeated_pending_stream_close_rejects_owner_callback_reentry(monkeypatch):
    from concurrent.futures import Future
    from types import SimpleNamespace

    from openeight.langchain._runtime import CURRENT_INVOCATION
    from openeight.langchain._sync import ManagedStream

    owner, _ = make(monkeypatch)
    with owner:
        stream = ManagedStream(owner._session._bridge, lambda: None)
        stream.closed = True
        stream.close_future = Future()
        monkeypatch.setattr(
            stream.close_future,
            "result",
            lambda: pytest.fail("blocking on own handler"),
        )
        token = CURRENT_INVOCATION.set(SimpleNamespace(owner=owner._session._owner))
        try:
            with pytest.raises(ConfigurationError) as error:
                stream.close()
            assert error.value.reason == "integration_sync_reentry"
        finally:
            CURRENT_INVOCATION.reset(token)
        stream.close_future.set_result(None)
