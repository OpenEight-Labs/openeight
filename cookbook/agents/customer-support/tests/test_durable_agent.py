"""Native adapter lifecycle with deterministic providers and local receipts.

These tests fake OpenEight transport to isolate SDK mechanics. Authority and
membership acceptance require the separate real authenticated HTTP harness.
"""

import asyncio
import pytest
from uuid import uuid4, uuid5, NAMESPACE_URL

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from openeight import (
    ToolReadiness,
    DecisionResult,
    FieldSelector,
    PolicyInputRequirement,
    RegistrationResult,
    Tool,
    ToolVersion,
    TransportError,
)
from openeight import OpenEightConfig
from openeight.langchain import create_agent
from openeight.langchain.durable import DurableApprovalConfig, ReconciliationResult
from helpers_remedies import confirm
from tools import build_tools
from local import Deployment, canonical, now
from retailer import RetailerRepository
from fixtures import seed_retailer
from storage import SQLiteExecutionStore


@pytest.fixture(autouse=True)
def offline_control():
    from tests.support.http_control import ControlHTTP

    with ControlHTTP(Transport) as control:
        yield control


class ToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class Transport:
    requests = []
    writes = []
    verdict = "allow"
    approval = "pending"
    lose_decide = False
    lose_observe = False

    def __init__(self, *args, **kwargs):
        pass

    def get_tool_readiness(self, ids, **kwargs):
        from types import SimpleNamespace
        from dataclasses import replace

        if not ids:
            return SimpleNamespace(tools=())
        results = tuple(self.catalog_versions.values())
        rows = []
        for result in results:
            ready = result.readiness
            if result.tool_version.id not in ids or ready is None:
                continue
            if ready.eligible and ready.policy_ready:
                ready = replace(
                    ready,
                    reason="ready",
                    policy_contract_set_digest="2" * 64,
                    policy_input_contracts=(
                        PolicyInputRequirement(
                            str(uuid5(NAMESPACE_URL, result.tool.name + "/binding")),
                            str(uuid5(NAMESPACE_URL, result.tool.name + "/policy")),
                            str(
                                uuid5(
                                    NAMESPACE_URL, result.tool.name + "/policy-version"
                                )
                            ),
                            "3" * 64,
                            tuple(
                                FieldSelector(k)
                                for k in result.tool_version.input_schema["properties"]
                            ),
                        ),
                    ),
                )
            rows.append(ready)
        return SimpleNamespace(tools=tuple(rows))

    def register_tools(self, facts, **kwargs):
        self.facts = tuple(facts)
        results = []
        for i, fact in enumerate(self.facts):
            identity = str(uuid5(NAMESPACE_URL, fact.name))
            version = ToolVersion(
                id=str(uuid5(NAMESPACE_URL, fact.name + "/v1")),
                tool_id=identity,
                version="config:test",
                content_digest="sha256:" + "0" * 64,
                input_schema=fact.input_schema,
                tool_contract_digest="sha256:" + "1" * 64,
                created_at="2026-10-05T00:00:00Z",
            )
            results.append(
                RegistrationResult(
                    i,
                    "replayed",
                    "",
                    Tool(
                        identity,
                        fact.name,
                        fact.description,
                        "write",
                        True,
                        "demo_retailer",
                        fact.name,
                        "2026-10-05T00:00:00Z",
                    ),
                    version,
                    ToolReadiness(
                        i,
                        fact.name,
                        identity,
                        version.id,
                        "registered",
                        True,
                        True,
                        True,
                        "",
                    ),
                )
            )
        return tuple(results)

    def decide(self, **request):
        replayed = any(
            r["decision_id"] == request["decision_id"] for r in self.requests
        )
        self.requests.append(request)
        if type(self).lose_decide:
            type(self).lose_decide = False
            raise TransportError()
        return DecisionResult(
            request["decision_id"], self.verdict, "", request["run_id"], replayed
        )

    def approval_state(self, *args, **kwargs):
        return self.approval

    def write_telemetry(self, **kwargs):
        self.writes.append(kwargs)
        if type(self).lose_observe and kwargs["events"]:
            type(self).lose_observe = False
            raise TransportError()

    def close(self):
        pass


@pytest.mark.parametrize(
    "approval_state,parallel,lost_decide,lost_observe",
    [
        ("approved", False, False, False),
        ("rejected", False, False, False),
        ("expired", False, False, False),
        ("approved", True, False, False),
        ("approved", False, True, False),
        ("approved", False, False, True),
    ],
)
def test_durable_approval_restart_replays_same_decision_and_action_key(
    tmp_path, monkeypatch, approval_state, parallel, lost_decide, lost_observe
):
    Transport.requests, Transport.writes = [], []
    Transport.verdict, Transport.approval = "approval_required", "pending"
    Transport.lose_decide, Transport.lose_observe = lost_decide, False
    state = tmp_path / "state"
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
    seed_retailer(state, d)
    retailer = RetailerRepository.bind(state, d, "case_damaged")
    action_id = confirm(retailer)
    store = SQLiteExecutionStore(state, d)
    store.initialize()
    command, _ = store.admit(
        {
            "kind": "start",
            "command_id": str(uuid4()),
            "case_id": "case_damaged",
            "message": "My order is damaged.",
        },
        case_id="case_damaged",
    )

    async def reconcile(attempt):
        receipt = retailer.lookup_receipt(attempt.idempotency_key)
        if receipt is None:
            return ReconciliationResult("not_executed")
        return ReconciliationResult(
            "confirmed",
            canonical({"content": receipt["result_json"], "status": "success"}),
        )

    async def classify(thread_id):
        return "waiting_customer"

    async def bind(responses, saver):
        owner = create_agent(
            model=ToolModel(responses=responses),
            tools=build_tools(retailer),
            checkpointer=saver,
            openeight=OpenEightConfig(
                agent_name="agent",
                endpoint=d.endpoint,
                credential="ctk_test",
                durable=DurableApprovalConfig(store, reconcile, classify),
            ),
        )
        return await owner.__aenter__()

    async def exercise():
        async with AsyncSqliteSaver.from_conn_string(str(store.path)) as saver:
            owner = await bind(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "primary-call",
                                # A note changes the revision a refund validates.
                                # Parallel recovery needs two independent writes.
                                "name": "add_case_note" if parallel else "issue_refund",
                                "args": (
                                    {
                                        "case_id": "case_damaged",
                                        "body": "First synthetic note",
                                    }
                                    if parallel
                                    else retailer.execution_arguments(action_id)
                                ),
                            }
                        ]
                        + (
                            [
                                {
                                    "id": "note-call",
                                    "name": "add_case_note",
                                    "args": {
                                        "case_id": "case_damaged",
                                        "body": "Synthetic note",
                                    },
                                }
                            ]
                            if parallel
                            else []
                        ),
                    )
                ],
                saver,
            )

            async def start():
                return await owner.ainvoke_durable(
                    {"messages": [{"role": "user", "content": "My order is damaged."}]},
                    thread_id=command.thread_id,
                    command_id=command.id,
                )

            if lost_decide:
                with pytest.raises(TransportError):
                    await start()
                assert store.load(command.thread_id).status == "reconciliation_required"
            else:
                result = await start()
                assert result.status == "waiting"
            assert (
                tuple(r for r in retailer.receipts() if r["kind"] != "confirmation")
                == ()
            )
            old = store.get_attempt(command.thread_id, "primary-call")
            old_parallel = (
                store.get_attempt(command.thread_id, "note-call") if parallel else None
            )
            await owner.aclose()
        Transport.approval = approval_state
        Transport.lose_observe = lost_observe
        resumed, _ = store.admit(
            {
                "kind": "resume",
                "command_id": str(uuid4()),
                "thread_id": command.thread_id,
            }
        )
        async with AsyncSqliteSaver.from_conn_string(str(store.path)) as saver:
            owner = await bind([AIMessage(content="Your refund is confirmed.")], saver)
            if lost_observe:
                with pytest.raises(TransportError):
                    await owner.aresume(
                        thread_id=command.thread_id, command_id=resumed.id
                    )
                assert (
                    len(
                        tuple(
                            r
                            for r in retailer.receipts()
                            if r["kind"] != "confirmation"
                        )
                    )
                    == 1
                )
                await owner.aclose()
                resumed, _ = store.admit(
                    {
                        "kind": "resume",
                        "command_id": str(uuid4()),
                        "thread_id": command.thread_id,
                    }
                )
                owner = await bind(
                    [AIMessage(content="Your refund is confirmed.")], saver
                )
            result = await owner.aresume(
                thread_id=command.thread_id, command_id=resumed.id
            )
            assert result.status == "waiting_customer"
            current = store.get_attempt(command.thread_id, "primary-call")
            assert (
                current.decision_id == old.decision_id
                and current.idempotency_key == old.idempotency_key
            )
            if parallel:
                other = store.get_attempt(command.thread_id, "note-call")
                assert other.decision_id == old_parallel.decision_id
                assert other.idempotency_key == old_parallel.idempotency_key
                assert other.decision_id != current.decision_id
                assert other.idempotency_key != current.idempotency_key
            assert len(
                tuple(r for r in retailer.receipts() if r["kind"] != "confirmation")
            ) == ((2 if parallel else 1) if approval_state == "approved" else 0), (
                current
            )
            grouped = {}
            for request in Transport.requests:
                prior = grouped.setdefault(request["decision_id"], request)
                assert prior == request
            assert len({r["run_id"] for r in Transport.requests}) == 1
            assert not any(
                r["status"] == "completed" for w in Transport.writes for r in w["runs"]
            )
            await owner.aclose()
        if approval_state == "approved":
            replied, _ = store.admit(
                {
                    "kind": "reply",
                    "command_id": str(uuid4()),
                    "thread_id": command.thread_id,
                    "message": "Thank you.",
                }
            )
            async with AsyncSqliteSaver.from_conn_string(str(store.path)) as saver:
                owner = await bind([AIMessage(content="You are welcome.")], saver)
                result = await owner.areply(
                    "Thank you.", thread_id=command.thread_id, command_id=replied.id
                )
                assert result.run_id == store.load(command.thread_id).run_id
                assert [
                    t["body"]
                    for t in store.transcript(command.thread_id)
                    if t["speaker"] == "customer"
                ] == ["My order is damaged.", "Thank you."]
                await owner.aclose()

    asyncio.run(exercise())


def test_sync_durable_facade_reopens_on_owned_saver_loop(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from openeight.managed import ManagedResources

    Transport.requests, Transport.writes = [], []
    Transport.verdict, Transport.approval = "approval_required", "pending"
    Transport.lose_decide = Transport.lose_observe = False
    state = tmp_path / "state"
    deployment = Deployment(
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
    seed_retailer(state, deployment)
    retailer = RetailerRepository.bind(state, deployment, "case_damaged")
    action_id = confirm(retailer)
    store = SQLiteExecutionStore(state, deployment)
    store.initialize()
    command, _ = store.admit(
        dict(
            kind="start",
            command_id=str(uuid4()),
            case_id="case_damaged",
            message="My order is damaged.",
        ),
        case_id="case_damaged",
    )
    resources_exited = []

    @asynccontextmanager
    async def resources():
        async with AsyncSqliteSaver.from_conn_string(str(store.path)) as saver:
            yield ManagedResources(checkpointer=saver)
        resources_exited.append(True)

    async def reconcile(attempt):
        receipt = retailer.lookup_receipt(attempt.idempotency_key)
        if receipt is None:
            return ReconciliationResult("not_executed")
        return ReconciliationResult(
            "confirmed",
            canonical(dict(content=receipt["result_json"], status="success")),
        )

    async def classify(_):
        return "waiting_customer"

    def owner(responses):
        return create_agent(
            ToolModel(responses=responses),
            tools=build_tools(retailer),
            resources=resources,
            openeight=OpenEightConfig(
                agent_name="agent",
                endpoint=deployment.endpoint,
                credential="ctk_test",
                durable=DurableApprovalConfig(store, reconcile, classify),
            ),
        )

    with owner(
        [
            AIMessage(
                content="",
                tool_calls=[
                    dict(
                        id="primary-call",
                        name="issue_refund",
                        args=retailer.execution_arguments(action_id),
                    )
                ],
            ),
            AIMessage(content="Handled."),
        ]
    ) as agent:
        waiting = agent.invoke_durable(
            {"messages": [{"role": "user", "content": "My order is damaged."}]},
            thread_id=command.thread_id,
            command_id=command.id,
        )
        assert waiting.status == "waiting"
        original = store.get_attempt(command.thread_id, "primary-call")
    assert resources_exited == [True]
    resumed, _ = store.admit(
        dict(kind="resume", command_id=str(uuid4()), thread_id=command.thread_id)
    )
    Transport.approval = "approved"
    with owner([AIMessage(content="Handled.")]) as agent:
        result = agent.resume(thread_id=command.thread_id, command_id=resumed.id)
        assert result.status == "waiting_customer" and result.run_id == waiting.run_id
        attempt = store.get_attempt(command.thread_id, "primary-call")
        assert (
            attempt.status == "succeeded"
            and attempt.decision_id == original.decision_id
        )
        assert attempt.idempotency_key == original.idempotency_key
    assert resources_exited == [True, True]
    assert len([r for r in retailer.receipts() if r["kind"] != "confirmation"]) == 1
