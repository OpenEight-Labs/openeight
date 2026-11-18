"""Public local status reads preserve durable identity and never advance execution."""

import asyncio
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

pytest.importorskip("deepagents")
pytest.importorskip("aiosqlite")

from test_deep_durable_engine import NativeClient, candidate_selection
from test_local_durable_facade import INPUT, candidate, clone_client, configure_catalog

from openeight import APIError, ConfigurationError
from openeight.deepagents import DurableThreadStatus
from openeight.langchain import _owner


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_status_reopens_current_thread_without_replaying_its_frozen_command(
    tmp_path, monkeypatch, mode
):
    candidate_selection(monkeypatch)
    client = NativeClient()
    client.state = "pending"
    clients = [client]
    monkeypatch.setattr(_owner, "Client", lambda *args, **kwargs: clients[-1])

    async def paused(agent):
        configure_catalog(client, agent)
        client.approval_names = {client.binding_names[(), "write_file"]}
        await agent.ainitialize()
        return await agent.astart(INPUT, command_id="start")

    if mode == "sync":
        with candidate(tmp_path) as agent:
            configure_catalog(client, agent)
            client.approval_names = {client.binding_names[(), "write_file"]}
            agent.initialize()
            waiting = agent.start(INPUT, command_id="start")
    else:

        async def first():
            async with candidate(tmp_path) as agent:
                return await paused(agent)

        waiting = asyncio.run(first())

    assert waiting.status == "waiting"
    approved = clone_client(client)
    approved.approval_names = client.approval_names
    clients.append(approved)

    def assert_waiting(status):
        assert status == DurableThreadStatus(
            waiting.thread_id, waiting.run_id, "waiting", waiting.pending_decision_ids
        )
        with pytest.raises(FrozenInstanceError):
            status.status = "completed"

    def assert_completed(status, replay):
        assert status == DurableThreadStatus(
            waiting.thread_id, waiting.run_id, "completed", ()
        )
        assert replay == waiting
        assert not hasattr(status, "output")
        assert approved.registrations == []

    if mode == "sync":
        with candidate(tmp_path) as agent:
            # Console approval is not journal settlement.
            assert_waiting(agent.status(thread_id=waiting.thread_id))
            agent.resume(thread_id=waiting.thread_id, command_id="resume")
            replay = agent.start(INPUT, command_id="start")
            requests = list(approved.requests)
            assert_completed(agent.status(thread_id=waiting.thread_id), replay)
            assert approved.requests == requests
    else:

        async def second():
            async with candidate(tmp_path) as agent:
                assert_waiting(await agent.astatus(thread_id=waiting.thread_id))
                await agent.aresume(thread_id=waiting.thread_id, command_id="resume")
                replay = await agent.astart(INPUT, command_id="start")
                requests = list(approved.requests)
                assert_completed(
                    await agent.astatus(thread_id=waiting.thread_id), replay
                )
                assert approved.requests == requests

        asyncio.run(second())


@pytest.mark.asyncio
async def test_status_of_interrupted_command_neither_recovers_nor_reads_checkpoints(
    tmp_path, monkeypatch
):
    candidate_selection(monkeypatch)
    client = NativeClient()
    monkeypatch.setattr(_owner, "Client", lambda *args, **kwargs: client)
    async with candidate(tmp_path) as agent:
        configure_catalog(client, agent)
        await agent.ainitialize()
        store = agent._local_runtime.store
        with store.acquire_deployment():
            admission = store.admit(kind="start", command_id="lost", message="go")
            with store.acquire(admission.thread_id):
                command = store.command(admission.thread_id, "lost")
                thread = store.load(admission.thread_id)
                store.begin_command(
                    thread.id, command.id, command.revision, thread.revision
                )
        before = (store.load(thread.id), store.command(thread.id, "lost"))
        registrations = list(client.registrations)

        async def forbidden(*args, **kwargs):
            pytest.fail("status must not read native checkpoints or invoke a model")

        monkeypatch.setattr(agent._owner._compatibility, "read_checkpoint", forbidden)
        monkeypatch.setattr(agent._owner._graph, "ainvoke", forbidden)
        snapshot = await agent.astatus(thread_id=thread.id)
        assert snapshot == DurableThreadStatus(thread.id, thread.run_id, "active", ())
        assert before == (store.load(thread.id), store.command(thread.id, "lost"))
        assert client.requests == [] and client.registrations == registrations
        with store._db() as db:
            assert db.execute("SELECT count(*) FROM commands").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unauthorized", "unavailable", "not_ready"])
async def test_status_authenticates_before_reading_thread(
    tmp_path, monkeypatch, failure
):
    from dataclasses import replace

    from openeight.errors import TransportError

    candidate_selection(monkeypatch)
    client = NativeClient()
    monkeypatch.setattr(_owner, "Client", lambda *args, **kwargs: client)
    async with candidate(tmp_path) as agent:
        configure_catalog(client, agent)
        await agent.ainitialize()
        result = await agent.astart(INPUT, command_id="start")
        original = client.get_tool_readiness

        def refuse(*args, **kwargs):
            if failure == "unauthorized":
                raise APIError(401, "unauthorized", "not authorized")
            if failure == "unavailable":
                raise TransportError()
            readiness = original(*args, **kwargs)
            return replace(
                readiness,
                tools=tuple(replace(tool, eligible=False) for tool in readiness.tools),
            )

        def forbidden(*args, **kwargs):
            pytest.fail("unauthorized or unavailable status must not read thread state")

        monkeypatch.setattr(client, "get_tool_readiness", refuse)
        monkeypatch.setattr(agent._local_runtime.store, "thread_status", forbidden)
        expected = {
            "unauthorized": APIError,
            "unavailable": TransportError,
            "not_ready": ConfigurationError,
        }[failure]
        with pytest.raises(expected):
            await agent.astatus(thread_id=result.thread_id)


@pytest.mark.asyncio
async def test_status_rejects_unbound_foreign_thread_and_busy_ownership(
    tmp_path, monkeypatch
):
    candidate_selection(monkeypatch)
    client = NativeClient()
    clients = [client]
    monkeypatch.setattr(_owner, "Client", lambda *args, **kwargs: clients[-1])
    async with candidate(tmp_path / "first") as first:
        configure_catalog(client, first)
        with pytest.raises(ConfigurationError) as unbound:
            await first.astatus(thread_id=str(uuid4()))
        assert unbound.value.reason == "durable_deployment_unbound"
        assert client.registrations == [] and client.readiness_calls == 0
        await first.ainitialize()
        result = await first.astart(INPUT, command_id="start")

    clients.append(clone_client(client))
    async with candidate(tmp_path / "second") as second:
        await second.ainitialize()
        with pytest.raises(ConfigurationError) as missing:
            await second.astatus(thread_id=result.thread_id)
        assert missing.value.reason == "durable_thread_missing"
        with second._local_runtime.store.locks.hold("deployment"):
            with pytest.raises(ConfigurationError) as busy:
                await second.astatus(thread_id=result.thread_id)
        assert busy.value.reason == "integration_busy"


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_id", [None, 42, "bad", str(uuid4()).upper()])
async def test_status_rejects_invalid_thread_selector_before_readiness(
    tmp_path, monkeypatch, thread_id
):
    candidate_selection(monkeypatch)
    client = NativeClient()
    monkeypatch.setattr(_owner, "Client", lambda *args, **kwargs: client)
    async with candidate(tmp_path) as agent:
        with pytest.raises(ConfigurationError) as invalid:
            await agent.astatus(thread_id=thread_id)
        assert invalid.value.reason == "invalid_input"
        assert client.readiness_calls == 0 and client.registrations == []
