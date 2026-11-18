"""Both public adapters retain cleanup and process boundaries on the owner loop."""

import asyncio
import json
import os
import select
import signal
import threading
import warnings
from contextlib import asynccontextmanager

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from test_enforcement_facade import GovernedClient
from test_framework_boundary import ToolModel
from test_managed_langchain import config

from openeight import ConfigurationError
from openeight.langchain import _owner, create_agent
from openeight.managed import ManagedResources

INPUT = {"messages": [("user", "go")]}


class HistoryModel(ToolModel):
    def _generate(self, messages, *args, **kwargs):
        index = min(sum(m.type == "ai" for m in messages), len(self.responses) - 1)
        return ChatResult(generations=[ChatGeneration(message=self.responses[index])])


def make(monkeypatch, integration, *, resources=None, tools=(), calls=(), children=()):
    create = create_agent
    client = GovernedClient()
    extra = {}
    if integration == "deepagents":
        pytest.importorskip("deepagents")
        from test_managed_deepagents import PolicyClient

        from openeight.deepagents import create_deep_agent

        create = create_deep_agent
        client = PolicyClient()
        extra = {"subagents": children}
    monkeypatch.setattr(_owner, "Client", lambda *a, **k: client)
    return create(
        HistoryModel(responses=[*calls, AIMessage(content="done")]),
        tools,
        openeight=config(),
        resources=resources,
        **extra,
    ), client


@pytest.mark.parametrize("integration", ["langchain", "deepagents"])
def test_failed_sync_batch_retains_execution_until_admitted_sibling_settles(
    monkeypatch, integration
):
    entered, failed, release, returned = (threading.Event() for _ in range(4))
    effects, errors = [], []

    @tool
    async def slow_effect() -> str:
        """Hold one governed handler until explicitly released."""
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.001)
        effects.append("settled")
        return "done"

    class BatchModel(ToolModel):
        async def _agenerate(self, messages, *args, **kwargs):
            text = next(
                message.content for message in messages if message.type == "human"
            )
            if text == "fail":
                while not entered.is_set():
                    await asyncio.sleep(0.001)
                failed.set()
                raise ValueError("batch model failure")
            if text == "slow" and messages[-1].type == "human":
                message = AIMessage(
                    content="",
                    tool_calls=[dict(name="slow_effect", args={}, id="slow")],
                )
            else:
                message = AIMessage(content="done")
            return ChatResult(generations=[ChatGeneration(message=message)])

    create, client = create_agent, GovernedClient()
    if integration == "deepagents":
        pytest.importorskip("deepagents")
        from test_managed_deepagents import PolicyClient

        from openeight.deepagents import create_deep_agent

        create, client = create_deep_agent, PolicyClient()
    monkeypatch.setattr(_owner, "Client", lambda *args, **kwargs: client)
    owner = create(
        BatchModel(responses=[AIMessage(content="done")]),
        [slow_effect],
        openeight=config(),
    )

    def execute():
        try:
            owner.batch(
                [
                    {"messages": [("user", "slow")]},
                    {"messages": [("user", "fail")]},
                ]
            )
        except BaseException as error:
            errors.append(error)
        finally:
            returned.set()

    with owner:
        caller = threading.Thread(target=execute)
        caller.start()
        try:
            assert entered.wait(5) and failed.wait(5)
            assert not returned.wait(0.05)
            assert owner._session._bridge.busy.locked() and not effects
            with pytest.raises(ConfigurationError) as caught:
                owner.invoke(INPUT)
            assert caught.value.reason == "integration_busy"
        finally:
            release.set()
            caller.join(5)
        assert not caller.is_alive()
        assert effects == ["settled"]
        assert len(errors) == 1 and isinstance(errors[0], ValueError)
        assert str(errors[0]) == "batch model failure"
        assert owner.invoke(INPUT)["messages"][-1].content == "done"


@pytest.mark.parametrize("integration", ["langchain", "deepagents"])
def test_factory_exit_timeout_then_failure_retains_one_exit_and_terminal_status(
    monkeypatch, integration
):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    exits = []

    @asynccontextmanager
    async def resources():
        try:
            yield ManagedResources()
        finally:
            exits.append(threading.get_ident())
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.001)
            finished.set()
            raise RuntimeError("private cleanup detail")

    owner, client = make(monkeypatch, integration, resources=resources)
    owner.__enter__()
    try:
        owner.invoke(INPUT)
        owner.flush()
        from openeight import CleanupIncompleteError

        with pytest.raises(CleanupIncompleteError):
            owner.close(timeout=0.1)
        assert not owner.delivery_status().closed
        assert started.wait(5)
        assert owner._session._bridge.thread.is_alive()
        before = len(client.requests)
        with pytest.raises(ConfigurationError):
            owner.invoke(INPUT)
        assert len(client.requests) == before
    finally:
        release.set()
    assert finished.wait(5)
    with pytest.raises(ConfigurationError) as caught:
        owner.close()
    assert caught.value.reason == "integration_resource_cleanup_failed"
    assert "private cleanup detail" not in str(caught.value)
    assert exits == [owner._session._bridge.thread.ident]
    assert not owner._session._bridge.thread.is_alive()
    status = owner.delivery_status()
    assert (
        not status.closed
        and status.last_error_code == "integration_resource_cleanup_failed"
    )
    for close in (owner.close, lambda: owner.__exit__(None, None, None)):
        with pytest.raises(ConfigurationError) as repeated:
            close()
        assert repeated.value.reason == "integration_resource_cleanup_failed"
    assert exits == [owner._session._bridge.thread.ident]
    assert owner.delivery_status() == status


@pytest.mark.parametrize("integration", ["langchain", "deepagents"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_partial_entry_validates_factory_result_and_exits_once_without_loop_leak(
    monkeypatch, integration, cleanup_fails
):
    exits = []

    @asynccontextmanager
    async def resources():
        try:
            yield object()
        finally:
            exits.append(True)
            if cleanup_fails:
                raise RuntimeError("private failed-entry cleanup")

    owner, client = make(monkeypatch, integration, resources=resources)
    previous_threads = set(threading.enumerate())
    with pytest.raises(ConfigurationError) as caught:
        owner.__enter__()
    assert caught.value.reason == "invalid_integration_configuration"
    assert not any(
        thread.name == "openeight-loop" and thread not in previous_threads
        for thread in threading.enumerate()
    )
    assert not client.registrations and not client.requests
    assert exits == [True]
    if cleanup_fails:
        assert caught.value.__cause__.reason == "integration_resource_cleanup_failed"
        status = owner.delivery_status()
        assert (
            not status.closed
            and status.last_error_code == "integration_resource_cleanup_failed"
        )
        for operation in (owner.close, lambda: owner.__exit__(None, None, None)):
            with pytest.raises(ConfigurationError) as repeated:
                operation()
            assert repeated.value.reason == "integration_resource_cleanup_failed"
    else:
        owner.__exit__(None, None, None)
    assert exits == [True]


@pytest.mark.parametrize("integration", ["langchain", "deepagents"])
def test_borrowed_loop_affine_future_is_not_migrated_to_the_sync_owner(
    monkeypatch, integration
):
    caller_loop = asyncio.new_event_loop()
    borrowed = caller_loop.create_future()
    entered = []

    @asynccontextmanager
    async def resources():
        entered.append(asyncio.get_running_loop())
        await borrowed
        yield ManagedResources()

    owner, client = make(monkeypatch, integration, resources=resources)
    try:
        with pytest.raises(RuntimeError, match="different loop"):
            owner.__enter__()
        assert len(entered) == 1 and entered[0] is not caller_loop
        assert entered[0].is_closed()
        assert not client.requests and not client.registrations
    finally:
        borrowed.cancel()
        caller_loop.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires an actual POSIX fork")
@pytest.mark.parametrize("integration", ["langchain", "deepagents"])
def test_forked_managed_facade_and_stream_fail_before_inherited_loop_or_dispatch(
    monkeypatch, integration
):
    owner, client = make(monkeypatch, integration)
    with owner:
        stream = owner.stream(INPUT)
        read_fd, write_fd = os.pipe()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            reasons = []
            for operation in (
                lambda: owner.invoke(INPUT),
                lambda: owner.batch([INPUT]),
                lambda: next(stream),
                stream.close,
                owner.catalog_status,
                owner.delivery_status,
                owner.close,
                lambda: owner.__exit__(None, None, None),
            ):
                try:
                    operation()
                except ConfigurationError as error:
                    reasons.append(error.reason)
                except BaseException as error:
                    reasons.append(type(error).__name__)
                else:
                    reasons.append("unexpected admission")
            os.write(write_fd, json.dumps(reasons).encode())
            os.close(write_fd)
            os._exit(0)
        os.close(write_fd)
        try:
            readable, _, _ = select.select([read_fd], [], [], 5)
            assert readable, "forked child waited on an inherited SDK lock/loop"
            assert (
                json.loads(os.read(read_fd, 8192))
                == ["instrumentation_process_changed"] * 8
            )
        finally:
            os.close(read_fd)
            try:
                waited, _ = os.waitpid(pid, os.WNOHANG)
                if waited == 0:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        assert not client.requests
        assert owner.invoke(INPUT)["messages"][-1].content == "done"
        stream.close()


@pytest.mark.parametrize("deny", [False, True])
@pytest.mark.parametrize("stream_mode", ["values", "updates", "messages"])
def test_deep_sync_batch_and_subgraph_stream_share_child_decisions(
    monkeypatch, deny, stream_mode
):
    effects = []

    @tool
    async def effect(value: str) -> str:
        """Record one authorized child effect."""
        effects.append(value)
        return value

    child = dict(
        name="worker",
        description="work",
        tools=[effect],
        model=HistoryModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        dict(name="effect", id="effect", args={"value": "once"})
                    ],
                ),
                AIMessage(content="child done"),
            ]
        ),
    )
    calls = [
        AIMessage(
            content="",
            tool_calls=[
                dict(
                    name="task",
                    id="delegate",
                    args={"subagent_type": "worker", "description": "work"},
                )
            ],
        )
    ]
    owner, client = make(monkeypatch, "deepagents", children=[child], calls=calls)
    with owner:
        binding = next(
            b
            for b in owner.catalog_snapshot().bindings
            if b["child_path"] == ("worker",) and b["native_name"] == "effect"
        )
        if deny:
            client.reject_name = binding["name"]
        results = owner.batch([INPUT, INPUT], {"max_concurrency": 2})
        assert [r["messages"][-1].content for r in results] == ["done", "done"]
        with owner.stream(INPUT, stream_mode=stream_mode, subgraphs=True) as stream:
            chunks = list(stream)
        assert chunks and all(isinstance(chunk[0], tuple) for chunk in chunks)
        assert len(client.requests) == 6
        assert len({request["decision_id"] for request in client.requests}) == 6
        assert effects == ([] if deny else ["once"] * 3)
    assert not owner._session._bridge.thread.is_alive()
