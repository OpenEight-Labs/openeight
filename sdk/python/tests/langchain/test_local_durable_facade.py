"""Qualified local facade composition and strict deployment identity."""

import subprocess
import sys

import pytest

pytest.importorskip("deepagents")
pytest.importorskip("aiosqlite")

from langchain_core.messages import AIMessage
from test_deep_durable_engine import HistoryModel, NativeClient, candidate_selection
from test_managed_langchain import config

from openeight import ConfigurationError
from openeight.deepagents import LocalDurability, ToolSurface, create_deep_agent
from openeight.langchain import _owner
from openeight.managed import LocalDurableAgent

INPUT = {"messages": [{"role": "user", "content": "go"}]}


def candidate(directory, *, revision="release-1", governance=None, **options):
    root = HistoryModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "file",
                        "name": "write_file",
                        "args": {"file_path": "/report.txt", "content": "local"},
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    child = dict(
        name="worker",
        description="work",
        system_prompt="work",
        model=HistoryModel(responses=[AIMessage(content="done")]),
        tools=(),
    )
    return create_deep_agent(
        root,
        openeight=governance or config(),
        durability=LocalDurability(directory=directory, revision=revision),
        subagents=[child],
        include_general_purpose=False,
        tool_surfaces=[
            ToolSurface((), ("read_file", "write_file", "task")),
            ToolSurface(("worker",), ("read_file", "write_file")),
        ],
        **options,
    )


def configure_catalog(client, agent):
    client.binding_names = {
        (b.child_path, b.native_name): b.facts.name
        for b in agent._owner.catalog.native.values()
    }
    client.read_names = {
        b.facts.name
        for b in agent._owner.catalog.native.values()
        if b.native_name == "read_file"
    }


def test_local_construction_matches_declared_qualification_without_io(tmp_path):
    from openeight._framework._registry import load_registry

    directory = tmp_path / "qualification"
    qualified = any(
        row["id"] == "deepagents-durable-01" for row in load_registry()["entries"]
    )
    if qualified:
        assert isinstance(candidate(directory), LocalDurableAgent)
    else:
        with pytest.raises(ConfigurationError) as caught:
            candidate(directory)
        assert caught.value.reason == "unsupported_framework_feature"
    assert not directory.exists()


@pytest.mark.parametrize(
    "option", ["state_schema", "context_schema", "interrupt_on", "name"]
)
def test_unqualified_native_configuration_rejects_before_local_state(
    tmp_path, monkeypatch, option
):
    candidate_selection(monkeypatch)
    directory = tmp_path / "unqualified"
    with pytest.raises(ConfigurationError) as caught:
        candidate(directory, **{option: "custom"})
    assert caught.value.reason == "unsupported_graph_shape"
    assert not directory.exists()


def test_local_facade_type_import_keeps_optional_sqlite_and_deep_modules_lazy():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from openeight.managed import LocalDurableAgent; assert 'deepagents' not in sys.modules; assert 'aiosqlite' not in sys.modules; assert 'langgraph.checkpoint.sqlite.aio' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_deep_durable_records_are_the_existing_shared_class_objects():
    import openeight.deepagents as deep
    import openeight.langchain as chain

    for name in (
        "DurableExecutionStore",
        "DurableCommand",
        "DurableThread",
        "DurableObserveRecord",
        "DurableTransition",
        "AttemptTransition",
        "AttemptSettlement",
        "CommandSettlement",
        "DurableApprovalConfig",
        "DurableAgentResult",
    ):
        assert getattr(deep, name) is getattr(chain, name)


@pytest.mark.asyncio
async def test_bound_read_requires_fresh_server_revision_stamp_before_dispatch(
    tmp_path, monkeypatch
):
    from dataclasses import replace

    candidate_selection(monkeypatch)
    client = NativeClient()
    monkeypatch.setattr(_owner, "Client", lambda *args, **kwargs: client)
    async with candidate(tmp_path) as agent:
        configure_catalog(client, agent)
        await agent.ainitialize()
        original = client.get_tool_readiness

        def old_server(*args, **kwargs):
            result = original(*args, **kwargs)
            return replace(
                result,
                tools=tuple(
                    replace(t, governance_revision_id=None) for t in result.tools
                ),
            )

        monkeypatch.setattr(client, "get_tool_readiness", old_server)
        with pytest.raises(ConfigurationError) as caught:
            await agent.astart(INPUT, command_id="start")
        assert caught.value.reason == "governance_not_ready"
        assert client.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["rejected", "expired"])
async def test_declined_or_expired_native_approval_does_not_apply_file_update(
    tmp_path, monkeypatch, state
):
    candidate_selection(monkeypatch)
    client = NativeClient()
    client.state = "pending"
    monkeypatch.setattr(_owner, "Client", lambda *args, **kwargs: client)
    async with candidate(tmp_path) as agent:
        configure_catalog(client, agent)
        client.approval_names = {client.binding_names[(), "write_file"]}
        await agent.ainitialize()
        paused = await agent.astart(INPUT, command_id="start")
        assert paused.status == "waiting"
        client.state = state
        result = await agent.aresume(thread_id=paused.thread_id, command_id="declined")
        assert result.status == "completed" and result.output == ("done",)
        (attempt,) = agent._local_runtime.store.attempts(paused.thread_id)
        assert (
            attempt.status == "blocked"
            and attempt.decision_id == paused.pending_decision_ids[0]
        )
        assert (
            agent._local_runtime.store.generated_state_receipt(
                paused.thread_id, attempt.tool_call_id
            )
            is None
        )
        snapshot = await agent._owner._graph.aget_state(
            {"configurable": {"thread_id": paused.thread_id}}
        )
        assert not snapshot.values.get("files")


@pytest.mark.asyncio
async def test_local_setup_is_explicit_start_is_admitted_and_duplicates_return_saved_result(
    tmp_path, monkeypatch
):
    candidate_selection(monkeypatch)
    client = NativeClient()
    monkeypatch.setattr(_owner, "Client", lambda *args, **kwargs: client)
    agent = candidate(tmp_path)
    assert isinstance(agent, LocalDurableAgent)
    async with agent:
        configure_catalog(client, agent)
        runtime = agent._local_runtime
        assert (
            runtime.store._deployment_id is None and not (tmp_path / "ready").exists()
        )
        assert client.registrations == [] and client.readiness_calls == 0
        with pytest.raises(ConfigurationError) as unbound:
            await agent.astart(INPUT, command_id="start")
        assert unbound.value.reason == "durable_deployment_unbound"
        await agent.ainitialize()
        assert (tmp_path / "ready").exists()
        registrations = len(client.registrations)
        await agent.ainitialize()
        assert len(client.registrations) == registrations
        readiness = await agent.areadiness()
        assert readiness.readiness.tools and readiness.unpublished == ()
        assert (await agent.arefresh_tools()).publication[
            "catalog_delivery_state"
        ] == "not_started"
        assert (
            not agent.catalog_snapshot().publication_complete
            and agent.catalog_snapshot().results == ()
        )
        assert len(client.registrations) == registrations
        result = await agent.astart(INPUT, command_id="start")
        assert result.status == "completed" and result.output == ("done",)
        assert await agent.astart(INPUT, command_id="start") == result
        assert len(client.requests) == 1 and len(client.registrations) == registrations
        with pytest.raises(ConfigurationError) as conflict:
            await agent.astart(
                {"messages": [{"role": "user", "content": "different"}]},
                command_id="start",
            )
        assert conflict.value.reason == "durable_command_conflict"
        with pytest.raises(ConfigurationError):
            await agent.abind_deployment()
        with pytest.raises(ConfigurationError):
            await agent.ainvoke_durable(
                INPUT, thread_id=result.thread_id, command_id="start"
            )
    with pytest.raises(ValueError):
        await runtime.resources.checkpointer.conn.execute("SELECT 1")


def test_local_sync_facade_owns_saver_loop_and_cleanup(tmp_path, monkeypatch):
    candidate_selection(monkeypatch)
    client = NativeClient()
    monkeypatch.setattr(_owner, "Client", lambda *args, **kwargs: client)
    agent = candidate(tmp_path)
    with agent:
        configure_catalog(client, agent)
        agent.initialize()
        first = agent.start(INPUT, command_id="stable")
        assert (
            first.status == "completed"
            and agent.start(INPUT, command_id="stable") == first
        )
        assert agent.readiness().unpublished == ()
        assert (
            agent.refresh_tools().publication["catalog_delivery_state"] == "not_started"
        )
        assert len(client.registrations) == 1
    assert agent._bridge.stopped and agent.delivery_status().closed


def clone_client(previous, *, state="approved"):
    client = NativeClient()
    client.publications = previous.publications.copy()
    client.contract_names = previous.contract_names.copy()
    client.read_names = previous.read_names.copy()
    client.state = state
    return client


@pytest.mark.asyncio
async def test_incompatible_reopen_makes_zero_catalog_mutations_and_old_approval_resumes(
    tmp_path, monkeypatch
):
    candidate_selection(monkeypatch)
    first_client = NativeClient()
    first_client.state = "pending"
    clients = [first_client]
    monkeypatch.setattr(_owner, "Client", lambda *args, **kwargs: clients[-1])
    async with candidate(tmp_path) as first:
        configure_catalog(first_client, first)
        first_client.approval_names = {first_client.binding_names[(), "write_file"]}
        await first.ainitialize()
        paused = await first.astart(INPUT, command_id="start")
        assert paused.status == "waiting"
        original = first._local_runtime.store.attempts(paused.thread_id)
    incompatible = clone_client(first_client)
    clients.append(incompatible)
    rejected = candidate(tmp_path, revision="different")
    with pytest.raises(ConfigurationError) as mismatch:
        async with rejected:
            pytest.fail("changed revision must not enter a bound deployment")
    assert mismatch.value.reason == "durable_deployment_mismatch"
    assert incompatible.registrations == [] and incompatible.readiness_calls == 0
    assert incompatible.closed and rejected._closed
    approved = clone_client(first_client)
    approved.approval_names = first_client.approval_names
    clients.append(approved)
    from openeight.deepagents import CatalogDescription

    async with candidate(
        tmp_path,
        catalog_descriptions=[
            CatalogDescription((), "write_file", "Operator-only catalog description")
        ],
    ) as recovered:
        assert recovered._local_runtime.store.attempts(paused.thread_id) == original
        await recovered.ainitialize()
        await recovered.arefresh_tools()
        result = await recovered.aresume(
            thread_id=paused.thread_id, command_id="resume"
        )
        assert result.status == "completed" and result.output == ("done",)
        attempts = recovered._local_runtime.store.attempts(paused.thread_id)
        assert len(attempts) == 1 and attempts[0].decision_id == original[0].decision_id
        assert attempts[0].status == "succeeded"
        assert approved.registrations == []


@pytest.mark.asyncio
async def test_unreturned_running_start_recovers_thread_identity_without_dispatch(
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
            admission = store.admit(kind="start", command_id="lost-start", message="go")
            with store.acquire(admission.thread_id):
                command, thread = (
                    store.command(admission.thread_id, "lost-start"),
                    store.load(admission.thread_id),
                )
                store.begin_command(
                    thread.id, command.id, command.revision, thread.revision
                )
        original_read = agent._owner._compatibility.read_checkpoint

        async def unexpected_read(*args, **kwargs):
            pytest.fail("running duplicate must not read checkpoint state")

        monkeypatch.setattr(
            agent._owner._compatibility, "read_checkpoint", unexpected_read
        )
        duplicate = await agent.astart(INPUT, command_id="lost-start")
        assert (
            duplicate.status == "recovery_required"
            and duplicate.thread_id == admission.thread_id
            and duplicate.run_id == admission.run_id
        )
        assert duplicate.output == () and client.requests == []
        monkeypatch.setattr(
            agent._owner._compatibility, "read_checkpoint", original_read
        )
        resumed = await agent.aresume(
            thread_id=duplicate.thread_id, command_id="recover"
        )
        assert resumed.status == "completed" and resumed.output == ("done",)
        assert await agent.astart(INPUT, command_id="lost-start") == resumed
        assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_setup_authentication_failure_does_not_enroll_or_publish(
    tmp_path, monkeypatch
):
    from openeight import APIError

    candidate_selection(monkeypatch)
    client = NativeClient()
    monkeypatch.setattr(_owner, "Client", lambda *args, **kwargs: client)
    async with candidate(tmp_path) as agent:

        def refuse(*args, **kwargs):
            raise APIError(401, "unauthorized", "not authorized")

        monkeypatch.setattr(client, "get_tool_readiness", refuse)
        with pytest.raises(APIError):
            await agent.ainitialize()
        assert agent._local_runtime.store._deployment_id is None
        assert not (tmp_path / "ready").exists() and client.registrations == []


@pytest.mark.asyncio
async def test_cancelled_initialize_and_bounded_close_retain_setup_ownership(
    tmp_path, monkeypatch
):
    import asyncio
    import threading

    candidate_selection(monkeypatch)
    client = NativeClient()
    monkeypatch.setattr(_owner, "Client", lambda *args, **kwargs: client)
    started, release = threading.Event(), threading.Event()
    original = client.register_tools

    def blocked(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(client, "register_tools", blocked)
    async with candidate(tmp_path) as agent:
        configure_catalog(client, agent)
        runtime = agent._local_runtime
        initialization = asyncio.create_task(agent.ainitialize())
        try:
            assert await asyncio.to_thread(started.wait, 3)
            initialization.cancel()
            await asyncio.sleep(0)
            assert not initialization.done()
            assert not (await agent.aclose(timeout=0.01)).closed
            assert agent._operation_tasks
            await runtime.resources.checkpointer.conn.execute("SELECT 1")
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await initialization
            assert not (tmp_path / "ready").exists()
            assert runtime.store.deployment().manifest_json is None
            assert (await agent.aclose()).closed
            with pytest.raises(ValueError):
                await runtime.resources.checkpointer.conn.execute("SELECT 1")
        finally:
            release.set()
            await asyncio.gather(initialization, return_exceptions=True)


@pytest.mark.asyncio
async def test_pre_result_local_admission_keeps_resources_open_without_a_root(
    tmp_path, monkeypatch
):
    import asyncio

    from openeight.deepagents import _admission

    candidate_selection(monkeypatch)
    client = NativeClient()
    monkeypatch.setattr(_owner, "Client", lambda *args, **kwargs: client)
    entered, release = asyncio.Event(), asyncio.Event()
    async with candidate(tmp_path) as agent:
        configure_catalog(client, agent)
        await agent.ainitialize()
        first = await agent.astart(INPUT, command_id="start")
        runtime = agent._local_runtime
        original = _admission._validate_durable_admission

        async def paused(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(_admission, "_validate_durable_admission", paused)
        duplicate = asyncio.create_task(agent.astart(INPUT, command_id="start"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert not agent._owner.roots
            assert not (await agent.aclose(timeout=0.01)).closed
            await runtime.resources.checkpointer.conn.execute("SELECT 1")
            assert not duplicate.done()
            release.set()
            with pytest.raises(ConfigurationError) as stopped:
                await duplicate
            assert stopped.value.reason == "integration_closed"
            assert runtime.store.command_result(first.thread_id, "start") == first
            assert (await agent.aclose()).closed
        finally:
            release.set()
            await asyncio.gather(duplicate, return_exceptions=True)


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_public_local_reply_after_reopen_keeps_run_and_duplicate_command_results(
    tmp_path, monkeypatch, mode
):
    import asyncio
    import json
    from typing import Annotated

    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import InjectedToolArg, tool

    from openeight.deepagents import ReconciliationResult

    candidate_selection(monkeypatch)
    turn = {"waiting": True}
    receipts = {}
    classifications = []

    @tool
    async def effect(
        value: str, idempotency_key: Annotated[str, InjectedToolArg]
    ) -> str:
        """Persist an explicitly confirmed synthetic action under the SDK key."""
        assert value == "confirmed"
        previous = receipts.setdefault(idempotency_key, "recorded")
        assert previous == "recorded"
        return previous

    async def reconcile(attempt):
        if attempt.idempotency_key not in receipts:
            return ReconciliationResult("not_executed")
        return ReconciliationResult(
            "confirmed",
            json.dumps(
                {"content": "recorded", "status": "success"},
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    async def classify(thread_id):
        classifications.append(thread_id)
        return "waiting_customer" if turn["waiting"] else "terminal"

    class CustomerModel(HistoryModel):
        def _generate(self, messages, *args, **kwargs):
            latest = max(
                i for i, message in enumerate(messages) if message.type == "human"
            )
            if messages[latest].content == "begin":
                turn["waiting"] = True
                response = AIMessage(content="Please confirm")
            elif not any(message.type == "tool" for message in messages[latest + 1 :]):
                turn["waiting"] = False
                response = AIMessage(
                    content="",
                    tool_calls=[
                        dict(
                            id="same-business-call",
                            name="effect",
                            args={"value": "confirmed"},
                        )
                    ],
                )
            else:
                response = AIMessage(content="Recorded")
            return ChatResult(generations=[ChatGeneration(message=response)])

    first_client = NativeClient()
    clients = [first_client]
    monkeypatch.setattr(_owner, "Client", lambda *args, **kwargs: clients[-1])

    def build():
        return create_deep_agent(
            CustomerModel(responses=[AIMessage(content="unused")]),
            [effect],
            openeight=config(),
            durability=LocalDurability(
                directory=tmp_path,
                revision="customer-turn-v1",
                reconcile=reconcile,
                classify_turn=classify,
            ),
            subagents=[
                dict(
                    name="worker",
                    description="unused static worker",
                    model=HistoryModel(responses=[AIMessage(content="done")]),
                    tools=[],
                )
            ],
            include_general_purpose=False,
            tool_surfaces=[
                ToolSurface((), ("read_file", "effect", "task")),
                ToolSurface(("worker",), ("read_file",)),
            ],
        )

    initial = {"messages": [{"role": "user", "content": "begin"}]}

    def check_initial(result):
        assert result.status == "waiting_customer" and result.output == (
            "Please confirm",
        )
        assert not receipts and not first_client.requests

    def check_reply(waiting, completed):
        assert completed.status == "completed" and completed.output == ("Recorded",)
        assert (
            completed.thread_id == waiting.thread_id
            and completed.run_id == waiting.run_id
        )
        assert len(receipts) == 1 and all(receipts)
        assert classifications == [waiting.thread_id, waiting.thread_id]
        assert clients[-1].registrations == [] and len(clients[-1].requests) == 1

    if mode == "sync":
        with build() as first:
            configure_catalog(first_client, first)
            first.initialize()
            waiting = first.start(initial, command_id="start")
            check_initial(waiting)
        clients.append(clone_client(first_client))
        with build() as reopened:
            completed = reopened.reply(
                "confirm", thread_id=waiting.thread_id, command_id="reply"
            )
            assert (
                reopened.reply(
                    "confirm", thread_id=waiting.thread_id, command_id="reply"
                )
                == completed
            )
            assert reopened.start(initial, command_id="start") == waiting
            with pytest.raises(ConfigurationError) as changed:
                reopened.reply(
                    "different", thread_id=waiting.thread_id, command_id="reply"
                )
            assert changed.value.reason == "durable_command_conflict"
            check_reply(waiting, completed)
    else:

        async def scenario():
            async with build() as first:
                configure_catalog(first_client, first)
                await first.ainitialize()
                waiting = await first.astart(initial, command_id="start")
                check_initial(waiting)
            clients.append(clone_client(first_client))
            async with build() as reopened:
                completed = await reopened.areply(
                    "confirm", thread_id=waiting.thread_id, command_id="reply"
                )
                assert (
                    await reopened.areply(
                        "confirm", thread_id=waiting.thread_id, command_id="reply"
                    )
                    == completed
                )
                assert await reopened.astart(initial, command_id="start") == waiting
                with pytest.raises(ConfigurationError) as changed:
                    await reopened.areply(
                        "different", thread_id=waiting.thread_id, command_id="reply"
                    )
                assert changed.value.reason == "durable_command_conflict"
                check_reply(waiting, completed)

        asyncio.run(scenario())


@pytest.mark.asyncio
async def test_native_terminal_event_lost_ack_retries_same_evidence_without_dispatch(
    tmp_path, monkeypatch
):
    candidate_selection(monkeypatch)
    client = NativeClient()
    monkeypatch.setattr(_owner, "Client", lambda *a, **kw: client)
    original = client.write_telemetry
    accepted = []
    lose_ack = True

    def delivery(**payload):
        nonlocal lose_ack
        original(**payload)
        terminal = [
            e
            for e in payload.get("events", ())
            if e.get("decision_id") and e.get("observation_kind") == "tool_call"
        ]
        accepted.extend(terminal)
        if terminal and lose_ack:
            lose_ack = False
            raise RuntimeError("synthetic lost acknowledgement")

    client.write_telemetry = delivery
    async with candidate(tmp_path) as agent:
        configure_catalog(client, agent)
        await agent.ainitialize()
        with pytest.raises(RuntimeError, match="synthetic lost acknowledgement"):
            await agent.astart(INPUT, command_id="start")
        store = agent._local_runtime.store
        (event,) = accepted
        with store.acquire_deployment():
            thread_id = store.admit(
                kind="start", command_id="start", message="go"
            ).thread_id
        (before,) = store.attempts(thread_id)
        assert before.status == "succeeded"
        assert any(record.id == before.event_id for record in store.pending(thread_id))
        decisions = len(client.requests)
        result = await agent.aresume(thread_id=thread_id, command_id="after-lost-ack")
        assert result.status == "completed" and result.run_id == event["run_id"]
        assert len(client.requests) == decisions
        assert accepted == [event, event]
        assert store.attempts(thread_id) == (before,)
        assert not store.pending(thread_id)
