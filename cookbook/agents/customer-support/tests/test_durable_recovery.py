"""Native checkpoint/command correspondence, cancellation and bounded evidence."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from openeight import ConfigurationError, DeliveryConfig, TransportError
from openeight import OpenEightConfig
from openeight.langchain import create_agent
from openeight.langchain.durable import DurableApprovalConfig, ReconciliationResult
from local import Deployment, LocalError, canonical, now
from retailer import RetailerRepository
from fixtures import seed_retailer
from storage import SQLiteExecutionStore
from tools import build_tools
from helpers_remedies import confirm
from test_durable_agent import ToolModel, Transport


class SimulatedCrash(RuntimeError):
    pass


@pytest.fixture
def runtime(tmp_path, monkeypatch, offline_control):
    Transport.requests, Transport.writes = [], []
    Transport.verdict, Transport.approval = "allow", "pending"
    Transport.lose_decide = Transport.lose_observe = False
    d = Deployment(
        "demo",
        "http://127.0.0.1:7777",
        "workspace",
        "agent",
        "agent-id",
        "key-id",
        "integrated",
        4,
        now(),
    )
    state = tmp_path / "state"
    seed_retailer(state, d)
    store = SQLiteExecutionStore(state, d)
    store.initialize()
    retailer = RetailerRepository.bind(state, d, "case_damaged")

    action_id = confirm(retailer)

    class Runtime:
        terminal = False
        tools = None

        def admit(self, kind="start", thread_id=None, message="Help"):
            request = {"kind": kind, "command_id": str(uuid4())}
            if kind == "start":
                request.update(case_id="case_damaged", message=message)
            else:
                request["thread_id"] = thread_id
                if kind == "reply":
                    request["message"] = message
            return store.admit(
                request, case_id="case_damaged" if kind == "start" else None
            )[0]

        @asynccontextmanager
        async def owner(
            self,
            responses=None,
            *,
            delivery=None,
            max_model_calls=16,
            max_tool_attempts=64,
            capture_content="none",
            redact_content=None,
        ):
            async def reconcile(attempt):
                receipt = retailer.lookup_receipt(attempt.idempotency_key)
                return (
                    ReconciliationResult("not_executed")
                    if receipt is None
                    else ReconciliationResult(
                        "confirmed",
                        canonical(
                            {"content": receipt["result_json"], "status": "success"}
                        ),
                    )
                )

            async def classify(thread_id):
                return "terminal" if self.terminal else "waiting_customer"

            async with AsyncSqliteSaver.from_conn_string(str(store.path)) as saver:
                self.saver = saver
                async with create_agent(
                    model=ToolModel(
                        responses=responses or [AIMessage(content="Which remedy?")]
                    ),
                    tools=self.tools or build_tools(retailer),
                    checkpointer=saver,
                    openeight=OpenEightConfig(
                        agent_name=d.agent_name,
                        endpoint=d.endpoint,
                        credential="ctk_test",
                        delivery=delivery,
                        capture_content=capture_content,
                        redact_content=redact_content,
                        durable=DurableApprovalConfig(
                            store,
                            reconcile,
                            classify,
                            max_model_calls=max_model_calls,
                            max_tool_attempts=max_tool_attempts,
                        ),
                    ),
                ) as owner:
                    yield owner

        async def snapshot(self, thread_id):
            from types import SimpleNamespace

            checkpoint = await self.saver.aget_tuple({"configurable": {"thread_id": thread_id}})
            return SimpleNamespace(values=checkpoint.checkpoint["channel_values"])

        async def invoke(self, owner, command):
            request = __import__("json").loads(command.request_json)
            if command.kind == "start":
                return await owner.ainvoke_durable(
                    {"messages": [{"role": "user", "content": request["message"]}]},
                    thread_id=command.thread_id,
                    command_id=command.id,
                )
            if command.kind == "reply":
                return await owner.areply(
                    request["message"],
                    thread_id=command.thread_id,
                    command_id=command.id,
                )
            return await owner.aresume(
                thread_id=command.thread_id, command_id=command.id
            )

    value = Runtime()
    value.store, value.retailer = store, retailer
    value.action_id = action_id
    return value


@pytest.mark.parametrize("kind", ["start", "reply"])
@pytest.mark.parametrize("boundary", ["begin", "input_record", "projection"])
def test_repeated_recovery_preserves_root_input_and_output(
    runtime, monkeypatch, kind, boundary
):
    async def exercise():
        command = runtime.admit()
        if kind == "reply":
            async with runtime.owner([AIMessage(content="First question")]) as owner:
                await runtime.invoke(owner, command)
            command = runtime.admit("reply", command.thread_id, "Refund please")
        store = runtime.store
        original = None
        method = "begin_command" if boundary == "begin" else "record_command_input"
        if boundary != "projection":
            original = getattr(store, method)

            def crash(*args, **kwargs):
                original(*args, **kwargs)
                raise SimulatedCrash()

            monkeypatch.setattr(store, method, crash)
        else:
            store.fault = (
                lambda point: (_ for _ in ()).throw(SimulatedCrash())
                if point == "before_command_settlement_commit"
                else None
            )
        async with runtime.owner(
            [AIMessage(content="Your preference is recorded")]
        ) as owner:
            with pytest.raises(SimulatedCrash):
                await runtime.invoke(owner, command)
        if original:
            monkeypatch.setattr(store, method, original)
        store.fault = lambda _: None
        first = runtime.admit("resume", command.thread_id)
        begin = store.begin_command

        def second_crash(*args, **kwargs):
            begin(*args, **kwargs)
            raise SimulatedCrash()

        monkeypatch.setattr(store, "begin_command", second_crash)
        async with runtime.owner() as owner:
            with pytest.raises(SimulatedCrash):
                await runtime.invoke(owner, first)
        monkeypatch.setattr(store, "begin_command", begin)
        second = runtime.admit("resume", command.thread_id)
        async with runtime.owner(
            [AIMessage(content="Your preference is recorded")]
        ) as owner:
            result = await runtime.invoke(owner, second)
            state = await runtime.snapshot(command.thread_id)
            assert sum(m.id == command.id for m in state.values["messages"]) == 1
        assert result.status == "waiting_customer"
        assert all(
            store.find_command(c.id).status == "completed"
            for c in (command, first, second)
        )
        turns = store.transcript(command.thread_id)
        expected = (
            ["Help", "Your preference is recorded"]
            if kind == "start"
            else [
                "Help",
                "First question",
                "Refund please",
                "Your preference is recorded",
            ]
        )
        assert [t["body"] for t in turns] == expected

    asyncio.run(exercise())


def test_completion_lost_ack_retries_exact_fixed_payload(runtime, monkeypatch):
    runtime.terminal = True
    original = Transport.write_telemetry
    lost = False

    def send(self, **kwargs):
        nonlocal lost
        original(self, **kwargs)
        if any(r["status"] == "completed" for r in kwargs["runs"]) and not lost:
            lost = True
            raise TransportError()

    monkeypatch.setattr(Transport, "write_telemetry", send)

    async def exercise():
        command = runtime.admit()
        async with runtime.owner() as owner:
            with pytest.raises(TransportError):
                await runtime.invoke(owner, command)
        ended = runtime.store.load(command.thread_id).run_ended_at
        resumed = runtime.admit("resume", command.thread_id)
        async with runtime.owner() as owner:
            result = await runtime.invoke(owner, resumed)
        completions = [
            r for w in Transport.writes for r in w["runs"] if r["status"] == "completed"
        ]
        assert len(completions) == 2 and completions[0] == completions[1]
        assert result.status == "completed"
        assert runtime.store.load(command.thread_id).run_ended_at == ended
        assert runtime.store.load(command.thread_id).model_calls == 1
        assert [t["body"] for t in runtime.store.transcript(command.thread_id)] == [
            "Help",
            "Which remedy?",
        ]

    asyncio.run(exercise())


def test_callback_capacity_drop_persists_incomplete(runtime):
    async def exercise():
        command = runtime.admit()
        async with runtime.owner(
            delivery=DeliveryConfig(
                max_records=2, max_bytes=1024, flush_records=1, flush_bytes=512
            )
        ) as owner:
            result = await runtime.invoke(owner, command)
        assert result.status == "waiting_customer"
        assert runtime.store.load(command.thread_id).observation_incomplete == 1

    asyncio.run(exercise())




def test_completed_projection_survives_refused_recovery(runtime):
    runtime.terminal = True

    async def exercise():
        command = runtime.admit()
        runtime.store.fault = (
            lambda point: (_ for _ in ()).throw(SimulatedCrash())
            if point == "before_command_settlement_commit"
            else None
        )
        async with runtime.owner() as owner:
            with pytest.raises(SimulatedCrash):
                await runtime.invoke(owner, command)
        ended = runtime.store.load(command.thread_id).run_ended_at
        assert ended and runtime.store.load(command.thread_id).status == "completed"
        runtime.store.fault = lambda _: None
        refused = runtime.admit("resume", command.thread_id)
        runtime.store.refuse_dispatch(refused.id)
        resumed = runtime.admit("resume", command.thread_id)
        async with runtime.owner() as owner:
            result = await runtime.invoke(owner, resumed)
        assert result.status == "completed"
        assert runtime.store.load(command.thread_id).run_ended_at == ended
        assert runtime.store.load(command.thread_id).model_calls == 1
        assert all(
            runtime.store.find_command(c.id).status == "completed"
            for c in (command, refused, resumed)
        )
        assert [t["body"] for t in runtime.store.transcript(command.thread_id)] == [
            "Help",
            "Which remedy?",
        ]

    asyncio.run(exercise())


def test_callback_persistence_failure_cannot_complete_run(runtime, monkeypatch):
    def fail(*args):
        raise LocalError("storage_unavailable")

    monkeypatch.setattr(runtime.store, "enqueue_callback_event", fail)

    async def exercise():
        command = runtime.admit()
        async with runtime.owner() as owner:
            with pytest.raises((LocalError, ConfigurationError)):
                await runtime.invoke(owner, command)
        thread = runtime.store.load(command.thread_id)
        assert thread.observation_incomplete == 1 and thread.run_ended_at is None
        assert thread.status == "reconciliation_required"

    asyncio.run(exercise())


def refund_call(runtime, **changes):
    arguments = runtime.retailer.execution_arguments(runtime.action_id)
    arguments.update(changes)
    return AIMessage(
        content="",
        tool_calls=[{"id": "refund", "name": "issue_refund", "args": arguments}],
    )


@pytest.mark.parametrize("verdict", ["deny", "dry_run_only"])
def test_blocked_verdict_has_no_receipt_or_execution_event(runtime, verdict):
    Transport.verdict = verdict

    async def exercise():
        command = runtime.admit()
        async with runtime.owner(
            [refund_call(runtime), AIMessage(content="Needs human help")]
        ) as owner:
            await runtime.invoke(owner, command)
        assert (
            tuple(r for r in runtime.retailer.receipts() if r["kind"] != "confirmation")
            == ()
        )
        attempt = runtime.store.get_attempt(command.thread_id, "refund")
        assert attempt.status == "blocked" and attempt.terminal_event_json is None
        assert not any(
            e.get("decision_id") for w in Transport.writes for e in w["events"]
        )

    asyncio.run(exercise())




def test_model_limit_stops_before_additional_model_work(runtime):
    async def exercise():
        command = runtime.admit()
        async with runtime.owner(
            [refund_call(runtime), AIMessage(content="unexpected")], max_model_calls=1
        ) as owner:
            with pytest.raises(ConfigurationError, match="limit"):
                await runtime.invoke(owner, command)
        assert runtime.store.load(command.thread_id).model_calls == 1
        assert (
            len(
                tuple(
                    r
                    for r in runtime.retailer.receipts()
                    if r["kind"] != "confirmation"
                )
            )
            == 1
        )
        assert runtime.store.load(command.thread_id).run_ended_at is None

    asyncio.run(exercise())


def test_repeated_cancellation_retains_handler_and_thread_ownership(runtime):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        tools = list(build_tools(runtime.retailer))
        refund = next(t for t in tools if t.name == "issue_refund")
        execute = refund.coroutine

        async def slow(**arguments):
            entered.set()
            await release.wait()
            return await execute(**arguments)

        refund.coroutine = slow
        runtime.tools = tools
        command = runtime.admit()
        async with runtime.owner(
            [refund_call(runtime), AIMessage(content="Confirmed")]
        ) as owner:
            task = asyncio.create_task(runtime.invoke(owner, command))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            with pytest.raises(LocalError, match="execution_busy"):
                runtime.admit("resume", command.thread_id)
            with pytest.raises(LocalError, match="execution_busy"):
                with runtime.store.locks.hold("lifecycle"):
                    pass
            assert (
                tuple(
                    r
                    for r in runtime.retailer.receipts()
                    if r["kind"] != "confirmation"
                )
                == ()
            )
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (
                len(
                    tuple(
                        r
                        for r in runtime.retailer.receipts()
                        if r["kind"] != "confirmation"
                    )
                )
                == 1
            )
        with runtime.store.acquire(command.thread_id):
            assert runtime.store.find_command(command.id).status == "completed"

    asyncio.run(exercise())


def test_configured_successor_does_not_rebind_pending_decision(runtime, monkeypatch):
    async def exercise():
        Transport.verdict = "approval_required"
        command = runtime.admit()
        async with runtime.owner([refund_call(runtime)]) as owner:
            assert (await runtime.invoke(owner, command)).status == "waiting"
        old = runtime.store.attempts(command.thread_id)[0]
        register = Transport.register_tools

        def successor(self, *args, **kwargs):
            results = []
            for row in register(self, *args, **kwargs):
                version = str(uuid4())
                results.append(replace(
                    row,
                    tool_version=replace(row.tool_version, id=version),
                    readiness=replace(row.readiness, tool_version_id=version),
                ))
            return tuple(results)

        monkeypatch.setattr(Transport, "register_tools", successor)
        Transport.approval = "approved"
        resume = runtime.admit("resume", command.thread_id)
        async with runtime.owner() as owner:
            await runtime.invoke(owner, resume)
        current = runtime.store.attempts(command.thread_id)[0]
        assert current.status == "succeeded"
        assert (
            current.decision_id,
            current.tool_version_id,
            current.idempotency_key,
        ) == (old.decision_id, old.tool_version_id, old.idempotency_key)
        assert {r["tool_version_id"] for r in Transport.requests} == {
            old.tool_version_id
        }
        assert (
            len(
                tuple(
                    r
                    for r in runtime.retailer.receipts()
                    if r["kind"] != "confirmation"
                )
            )
            == 1
        )

    asyncio.run(exercise())


def test_pending_tool_configuration_returns_feedback_without_effect(
    runtime, monkeypatch
):
    register = Transport.register_tools

    def pending(self, *args, **kwargs):
        return tuple(
            replace(
                r,
                tool_version=replace(r.tool_version, tool_contract_digest=None),
                readiness=replace(
                    r.readiness,
                    policy_ready=False,
                    eligible=False,
                    reason="governance_unconfigured",
                ),
            )
            for r in register(self, *args, **kwargs)
        )

    monkeypatch.setattr(Transport, "register_tools", pending)

    async def exercise():
        command = runtime.admit()
        async with runtime.owner(
            [
                refund_call(runtime),
                AIMessage(
                    content="This action needs governance configuration before I can perform it."
                ),
            ]
        ) as owner:
            result = await runtime.invoke(owner, command)
        assert result.status == "waiting_customer"
        assert runtime.store.attempts(command.thread_id) == ()
        assert (
            tuple(r for r in runtime.retailer.receipts() if r["kind"] != "confirmation")
            == ()
            and Transport.requests == []
        )

    asyncio.run(exercise())


def test_outage_turn_reaches_next_model_once(runtime):
    from langchain_core.messages import HumanMessage

    async def exercise():
        first = runtime.admit(message="My parcel is missing")
        assert runtime.store.unavailable(first.id)
        second = runtime.admit("reply", first.thread_id, "Please try again")
        async with runtime.owner() as owner:
            await runtime.invoke(owner, second)
            snapshot = await runtime.snapshot(first.thread_id)
            humans = [
                m.content
                for m in snapshot.values["messages"]
                if isinstance(m, HumanMessage)
            ]
            assert humans == ["My parcel is missing", "Please try again"]
        third = runtime.admit("reply", first.thread_id, "Any update?")
        async with runtime.owner() as owner:
            await runtime.invoke(owner, third)
            snapshot = await runtime.snapshot(first.thread_id)
            humans = [
                m.content
                for m in snapshot.values["messages"]
                if isinstance(m, HumanMessage)
            ]
            assert humans == ["My parcel is missing", "Please try again", "Any update?"]

    asyncio.run(exercise())


def test_pristine_running_reply_can_project_outage_after_settled_effect(runtime):
    async def exercise():
        first = runtime.admit()
        async with runtime.owner(
            [refund_call(runtime), AIMessage(content="Refund confirmed")]
        ) as owner:
            await runtime.invoke(owner, first)
        second = runtime.admit("reply", first.thread_id, "I have another question")
        thread = runtime.store.load(first.thread_id)
        with runtime.store.acquire(first.thread_id):
            running = runtime.store.begin_command(
                first.thread_id, second.id, second.revision, thread.revision
            )
        assert runtime.store.unavailable(running.id)
        assert runtime.store.load(first.thread_id).active_command_id is None
        assert (
            len(
                tuple(
                    r
                    for r in runtime.retailer.receipts()
                    if r["kind"] != "confirmation"
                )
            )
            == 1
        )

    asyncio.run(exercise())


def test_pristine_reply_projects_outage_after_denied_attempt(runtime):
    async def exercise():
        Transport.verdict = "deny"
        first = runtime.admit()
        async with runtime.owner(
            [refund_call(runtime), AIMessage(content="I can offer another option")]
        ) as owner:
            await runtime.invoke(owner, first)
        assert runtime.store.attempts(first.thread_id)[0].status == "blocked"
        second = runtime.admit(
            "reply", first.thread_id, "Could we discuss replacement?"
        )
        assert runtime.store.unavailable(second.id)
        assert runtime.store.load(first.thread_id).status == "waiting_customer"
        assert not tuple(
            r for r in runtime.retailer.receipts() if r["kind"] != "confirmation"
        )

    asyncio.run(exercise())


def test_integrated_content_blocks_publish_only_visible_text(runtime):
    async def exercise():
        command = runtime.admit()
        async with runtime.owner(
            [
                AIMessage(
                    content=[
                        {"type": "reasoning", "reasoning": "private reasoning canary"},
                        {"type": "text", "text": "What happened with your order?"},
                    ]
                )
            ]
        ) as owner:
            result = await runtime.invoke(owner, command)
            assert result.output == ("What happened with your order?",)
        assert (
            runtime.store.transcript(command.thread_id)[-1]["body"]
            == "What happened with your order?"
        )

    asyncio.run(exercise())


@pytest.mark.parametrize("readiness", ["ineligible", "policy_not_ready"])
def test_configured_but_not_ready_returns_feedback_without_effect(
    runtime, monkeypatch, readiness
):
    register = Transport.register_tools

    def not_ready(self, *args, **kwargs):
        results = register(self, *args, **kwargs)
        assert all(result.tool_version.tool_contract_digest for result in results)
        return tuple(
            replace(
                result,
                readiness=None
                if readiness == "missing"
                else replace(
                    result.readiness,
                    eligible=readiness != "ineligible",
                    policy_ready=False,
                    reason="policy_not_ready",
                ),
            )
            for result in results
        )

    monkeypatch.setattr(Transport, "register_tools", not_ready)

    async def exercise():
        command = runtime.admit()
        async with runtime.owner(
            [refund_call(runtime), AIMessage(content="Needs governance")]
        ) as owner:
            result = await runtime.invoke(owner, command)
            snapshot = await runtime.snapshot(command.thread_id)
        from langchain_core.messages import ToolMessage

        feedback = [
            message
            for message in snapshot.values["messages"]
            if isinstance(message, ToolMessage)
        ]
        assert len(feedback) == 1 and feedback[0].status == "error"
        assert (
            "awaiting governance configuration or policy readiness"
            in feedback[0].content
        )
        assert result.status == "waiting_customer"
        assert runtime.store.attempts(command.thread_id) == ()
        assert (
            tuple(r for r in runtime.retailer.receipts() if r["kind"] != "confirmation")
            == ()
            and Transport.requests == []
        )

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["exception", "error_result", "settlement_crash"])
def test_unkeyed_uncertain_effect_never_reexecutes_or_claims_no_effect(
    runtime, failure
):
    from langchain_core.tools import tool
    from langchain_core.messages import ToolMessage

    effects = []

    @tool
    async def unkeyed(value: str) -> str:
        """Represent an external effect without a reconciliation key."""
        effects.append(value)
        if failure == "exception":
            raise RuntimeError("response lost after effect")
        if failure == "error_result":
            return ToolMessage(
                content="response unavailable", status="error", tool_call_id="unkeyed"
            )
        return "effect completed"

    runtime.tools = [unkeyed]
    call = AIMessage(
        content="",
        tool_calls=[{"id": "unkeyed", "name": "unkeyed", "args": {"value": "once"}}],
    )

    async def exercise():
        command = runtime.admit()
        if failure == "settlement_crash":

            def crash(point):
                if point == "after_attempt_result":
                    raise SimulatedCrash()

            runtime.store.fault = crash
        async with runtime.owner([call, AIMessage(content="done")]) as owner:
            with pytest.raises((ConfigurationError, SimulatedCrash)) as error:
                await runtime.invoke(owner, command)
            if failure != "settlement_crash":
                assert error.value.reason == "durable_reconciliation_required"
        runtime.store.fault = lambda _: None
        assert effects == ["once"] and len(Transport.requests) == 1
        for _ in range(2):
            resume = runtime.admit("resume", command.thread_id)
            async with runtime.owner() as owner:
                with pytest.raises(ConfigurationError) as error:
                    await runtime.invoke(owner, resume)
            assert error.value.reason == "durable_reconciliation_required"
            attempt = runtime.store.get_attempt(command.thread_id, "unkeyed")
            assert attempt.status == "executing" and attempt.idempotency_key is None
            assert attempt.result_json is None and attempt.terminal_event_json is None
            assert (
                runtime.store.load(command.thread_id).status
                == "reconciliation_required"
            )
            assert effects == ["once"] and len(Transport.requests) == 1
            assert not any(
                event.get("decision_id") == attempt.decision_id
                for batch in Transport.writes
                for event in batch["events"]
            )

    asyncio.run(exercise())


def test_read_budget_survives_reply_and_owner_restart(runtime, monkeypatch):
    original = Transport.register_tools

    def register_reads(self, facts, **kwargs):
        return tuple(
            replace(
                row,
                tool=replace(row.tool, capability="read"),
                readiness=replace(row.readiness, reason="read_only"),
            )
            for row in original(self, facts, **kwargs)
        )

    monkeypatch.setattr(Transport, "register_tools", register_reads)

    def read_call(identity):
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": identity,
                    "name": "get_order",
                    "args": {"order_id": "order_damaged"},
                }
            ],
        )

    async def exercise():
        command = runtime.admit()
        async with runtime.owner(
            [read_call("first"), AIMessage(content="Anything else?")],
            max_tool_attempts=1,
        ) as owner:
            await runtime.invoke(owner, command)
        thread = runtime.store.load(command.thread_id)
        assert thread.read_calls == 1
        assert runtime.store.attempts(thread.id) == ()
        assert Transport.requests == []
        reply = runtime.admit("reply", thread.id, "Read again")
        async with runtime.owner(
            [read_call("second"), AIMessage(content="Done")], max_tool_attempts=1
        ) as owner:
            with pytest.raises(ConfigurationError) as caught:
                await runtime.invoke(owner, reply)
            assert caught.value.reason == "durable_limit_exhausted"
        assert runtime.store.load(thread.id).read_calls == 1
        assert Transport.requests == []

    asyncio.run(exercise())
