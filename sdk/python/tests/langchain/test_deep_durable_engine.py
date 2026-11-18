"""Deterministic native engine tests with a fixed test qualification identity."""

from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

import pytest

pytest.importorskip("deepagents")
pytest.importorskip("aiosqlite")

from _advanced_fixture import open_advanced_durability
from langchain_core.messages import AIMessage
from test_framework_boundary import ToolModel
from test_managed_deepagents import PolicyClient

from openeight import (
    AgentReadiness,
    PolicyInputRequirement,
    ToolReadiness,
    _framework,
)
from openeight.deepagents._deployment import canonical, credential_binding
from openeight.deepagents.durable import DeepAgentsDeploymentConfig, LocalDurability
from openeight.langchain import _durable_execution, _owner
from openeight.langchain.durable import DurableApprovalConfig, ReconciliationResult

VERIFIED_IDENTITY = dict(
    kind="verified_issuance",
    endpoint="http://localhost:7777",
    agent_name="test",
    **{
        field: str(uuid5(NAMESPACE_URL, "native-fixture/" + field))
        for field in ("organization_id", "workspace_id", "agent_id", "api_key_id")
    },
)


class NativeClient(PolicyClient):
    approval_names = frozenset()

    def decide(self, **request):
        result = super().decide(**request)
        name = self.contract_names[request["tool_id"]]
        return replace(
            result,
            verdict="approval_required" if name in self.approval_names else "allow",
        )

    def approval_state(self, decision_id, **kwargs):
        return getattr(self, "approval_states", {}).get(decision_id, self.state)

    def get_tool_readiness(self, versions, **kwargs):
        self.readiness_calls += 1
        values = []
        for version in versions:
            publication = self.publications[version]
            requirement = PolicyInputRequirement(
                *(
                    str(uuid5(NAMESPACE_URL, version + label))
                    for label in ("binding", "policy", "policy-version")
                ),
                "b" * 64,
                (),
            )
            if publication.tool.name in getattr(self, "read_names", set()):
                values.append(
                    ToolReadiness(
                        None,
                        publication.tool.name,
                        publication.tool.id,
                        version,
                        "registered",
                        False,
                        True,
                        True,
                        "read_only",
                        governance_revision_id=str(
                            uuid5(NAMESPACE_URL, version + "revision")
                        ),
                    )
                )
                continue
            values.append(
                ToolReadiness(
                    None,
                    publication.tool.name,
                    publication.tool.id,
                    version,
                    "registered",
                    True,
                    True,
                    True,
                    "ready",
                    "a" * 64,
                    (requirement,),
                )
            )
        return AgentReadiness(tuple(values), True, "registered", True, "ready")


class HistoryModel(ToolModel):
    files_by_description: bool = False

    def _generate(self, messages, *args, **kwargs):
        from langchain_core.outputs import ChatGeneration, ChatResult

        index = sum(message.type == "ai" for message in messages)
        message = self.responses[min(index, len(self.responses) - 1)].model_copy(
            deep=True
        )
        if self.files_by_description and message.tool_calls:
            label = next(m.content for m in messages if m.type == "human")
            message.tool_calls[0]["args"] = {
                "file_path": "/" + label + ".txt",
                "content": label,
            }
        return ChatResult(generations=[ChatGeneration(message=message)])


def candidate_selection(monkeypatch):
    # Freeze a separate identity for manifest-mismatch and engine-isolation tests.
    selection = replace(
        _framework.select_compatibility(
            _framework.detect_framework("deepagents"), required=frozenset({"ordinary"})
        ),
        qualification_id="test-only-native-candidate",
        checkpoint_format="test-only-deep-sqlite-v1",
        supported_features=frozenset({"durable", "local_durable", "sync"}),
    )
    monkeypatch.setattr(
        _framework, "select_compatibility", lambda *args, **kwargs: selection
    )
    monkeypatch.setattr(
        _durable_execution, "select_compatibility", lambda *args, **kwargs: selection
    )
    from openeight.deepagents import _builder

    monkeypatch.setattr(
        _builder, "select_compatibility", lambda *args, **kwargs: selection
    )


def candidate_agent(
    runtime, deployment, responses, *, client, children=(), resources=None
):
    if not children:
        children = (
            {
                "name": "worker",
                "description": "work",
                "system_prompt": "work",
                "model": ToolModel(responses=[AIMessage(content="done")]),
                "tools": (),
                "middleware": (),
                "skills": (),
            },
        )

    async def reconcile(_):
        return ReconciliationResult("unknown")

    async def classify(_):
        return "terminal"

    from openeight import OpenEightConfig
    from openeight.deepagents import ToolSurface, create_deep_agent

    agent = create_deep_agent(
        HistoryModel(responses=responses),
        openeight=OpenEightConfig(
            agent_name="test",
            endpoint="http://localhost:7777",
            credential="ctk_test",
            durable=DurableApprovalConfig(
                store=runtime.store, reconcile=reconcile, classify_turn=classify
            ),
        ),
        subagents=children,
        tool_surfaces=[
            ToolSurface((), ("read_file", "write_file", "task")),
            *(
                ToolSurface(
                    (child["name"],),
                    tuple(
                        sorted(
                            {"read_file", "write_file"}
                            | {tool.name for tool in child.get("tools", ())}
                        )
                    ),
                )
                for child in children
            ),
        ],
        catalog_naming="hashed-v1",
        include_general_purpose=False,
        durable_deployment=DeepAgentsDeploymentConfig(
            deployment.deployment_id, "a" * 64, "b" * 64
        ),
        **(
            {"resources": resources}
            if resources is not None
            else {
                "checkpointer": runtime.resources.checkpointer,
                "store": runtime.resources.store,
            }
        ),
    )
    build = agent._build

    def capture_external_contract_names(*args, **kwargs):
        built = build(*args, **kwargs)
        client.binding_names = {
            (binding.child_path, binding.native_name): binding.facts.name
            for binding in built.bindings.native.values()
        }
        client.read_names = {
            binding.facts.name
            for binding in built.bindings.native.values()
            if binding.native_name == "read_file"
        }
        return built

    agent._build = capture_external_contract_names
    return agent


@pytest.mark.asyncio
async def test_private_candidate_file_call_uses_shared_decision_and_atomic_receipt(
    tmp_path, monkeypatch
):
    candidate_selection(monkeypatch)
    client = NativeClient()
    monkeypatch.setattr(_owner, "Client", lambda *args, **kwargs: client)
    async with open_advanced_durability(
        LocalDurability(directory=tmp_path, revision="candidate")
    ) as runtime:
        with runtime.store.acquire_deployment():
            deployment = runtime.store.initialize_enrollment(
                canonical(VERIFIED_IDENTITY), credential_binding("ctk_test")
            )
        responses = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "file",
                        "name": "write_file",
                        "args": {"file_path": "/report.txt", "content": "preserved"},
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
        async with candidate_agent(
            runtime, deployment, responses, client=client
        ) as agent:
            await agent.abind_deployment()
            registrations = len(client.registrations)
            with runtime.store.acquire_deployment():
                admission = runtime.store.admit(
                    kind="start", command_id="start", message="go"
                )
            result = await agent.ainvoke_durable(
                {"messages": [{"role": "user", "content": "go"}]},
                thread_id=admission.thread_id,
                command_id="start",
            )
            assert result.status == "completed" and result.output == ("done",)
            attempts = runtime.store.attempts(admission.thread_id)
            assert len(attempts) == 1 and attempts[0].status == "succeeded"
            receipt = runtime.store.generated_state_receipt(
                admission.thread_id, attempts[0].tool_call_id
            )
            assert '"content":"preserved"' in receipt.update_json
            assert (
                len(client.requests) == 1 and len(client.registrations) == registrations
            )
            snapshot = await agent._owner._graph.aget_state(
                {"configurable": {"thread_id": admission.thread_id}}
            )
            assert snapshot.values["files"]["/report.txt"]["content"] == "preserved"
            events = [
                event for write in client.writes for event in write.get("events", [])
            ]
            assert (
                sum(
                    event.get("decision_id") == attempts[0].decision_id
                    and event["observation_kind"] == "tool_call"
                    for event in events
                )
                == 1
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [(), ("worker",)])
async def test_private_candidate_root_and_child_approval_resume_through_shared_gate(
    tmp_path, monkeypatch, path
):
    candidate_selection(monkeypatch)
    client = NativeClient()
    client.state = "pending"
    monkeypatch.setattr(_owner, "Client", lambda *args, **kwargs: client)
    file_call = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "file",
                "name": "write_file",
                "args": {"file_path": "/approval.txt", "content": "once"},
            }
        ],
    )
    children = ()
    responses = [file_call, AIMessage(content="done")]
    if path:
        children = (
            {
                "name": "worker",
                "description": "work",
                "system_prompt": "work",
                "model": ToolModel(
                    responses=[file_call, AIMessage(content="child done")]
                ),
                "tools": (),
                "middleware": (),
                "skills": (),
            },
        )
        responses = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "delegate",
                        "name": "task",
                        "args": {"description": "work", "subagent_type": "worker"},
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    async with open_advanced_durability(
        LocalDurability(directory=tmp_path, revision="candidate")
    ) as runtime:
        with runtime.store.acquire_deployment():
            deployment = runtime.store.initialize_enrollment(
                canonical(VERIFIED_IDENTITY), credential_binding("ctk_test")
            )
        async with candidate_agent(
            runtime, deployment, responses, client=client, children=children
        ) as agent:
            client.approval_names = {client.binding_names[path, "write_file"]}
            await agent.abind_deployment()
            registrations = len(client.registrations)
            with runtime.store.acquire_deployment():
                admission = runtime.store.admit(
                    kind="start", command_id="start", message="go"
                )
            paused = await agent.ainvoke_durable(
                {"messages": [{"role": "user", "content": "go"}]},
                thread_id=admission.thread_id,
                command_id="start",
            )
            assert paused.status == "waiting" and len(paused.pending_decision_ids) == 1
            before = runtime.store.attempts(admission.thread_id)
            leaf = next(a for a in before if a.status == "waiting")
            assert all(
                runtime.store.generated_state_receipt(
                    admission.thread_id, a.tool_call_id
                )
                is None
                for a in before
            )
            if path:
                assert sum(a.status == "executing" for a in before) == 1
            client.state = "approved"
            with runtime.store.acquire_deployment():
                runtime.store.admit(
                    kind="resume", command_id="resume", thread_id=admission.thread_id
                )
            result = await agent.aresume(
                thread_id=admission.thread_id, command_id="resume"
            )
            assert result.status == "completed" and result.output == ("done",)
            after = runtime.store.attempts(admission.thread_id)
            assert {a.decision_id for a in after} == {a.decision_id for a in before}
            assert all(a.status == "succeeded" for a in after)
            assert (
                sum(r["decision_id"] == leaf.decision_id for r in client.requests) >= 3
            )
            snapshot = await agent._owner._graph.aget_state(
                {"configurable": {"thread_id": admission.thread_id}}
            )
            assert snapshot.values["files"]["/approval.txt"]["content"] == "once"
            assert len(client.registrations) == registrations


@pytest.mark.asyncio
async def test_private_candidate_child_business_write_reuses_exact_hidden_key_on_resume(
    tmp_path, monkeypatch
):
    import json
    from typing import Annotated

    from langchain_core.tools import InjectedToolArg, tool

    effects = []

    @tool
    async def effect(
        value: str, idempotency_key: Annotated[str, InjectedToolArg]
    ) -> str:
        """Apply one business action with a destination idempotency key."""
        effects.append((value, idempotency_key))
        return "receipt"

    candidate_selection(monkeypatch)
    client = NativeClient()
    client.state = "pending"
    monkeypatch.setattr(_owner, "Client", lambda *args, **kwargs: client)
    children = (
        {
            "name": "worker",
            "description": "work",
            "system_prompt": "work",
            "model": ToolModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "child-call",
                                "name": "effect",
                                "args": {"value": "once"},
                            }
                        ],
                    ),
                    AIMessage(content="child done"),
                ]
            ),
            "tools": (effect,),
            "middleware": (),
            "skills": (),
        },
    )
    responses = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "delegate",
                    "name": "task",
                    "args": {"description": "work", "subagent_type": "worker"},
                }
            ],
        ),
        AIMessage(content="done"),
    ]
    async with open_advanced_durability(
        LocalDurability(directory=tmp_path, revision="candidate")
    ) as runtime:
        with runtime.store.acquire_deployment():
            deployment = runtime.store.initialize_enrollment(
                canonical(VERIFIED_IDENTITY), credential_binding("ctk_test")
            )
        async with candidate_agent(
            runtime, deployment, responses, client=client, children=children
        ) as agent:
            client.approval_names = {client.binding_names[("worker",), "effect"]}
            await agent.abind_deployment()
            registrations = len(client.registrations)
            with runtime.store.acquire_deployment():
                admission = runtime.store.admit(
                    kind="start", command_id="start", message="go"
                )
            paused = await agent.ainvoke_durable(
                {"messages": [{"role": "user", "content": "go"}]},
                thread_id=admission.thread_id,
                command_id="start",
            )
            assert paused.status == "waiting" and effects == []
            leaf = next(
                a
                for a in runtime.store.attempts(admission.thread_id)
                if a.status == "waiting"
            )
            assert (
                json.loads(leaf.native_call_json)["args"]["idempotency_key"]
                == leaf.idempotency_key
            )
            client.state = "approved"
            with runtime.store.acquire_deployment():
                runtime.store.admit(
                    kind="resume", command_id="resume", thread_id=admission.thread_id
                )
            result = await agent.aresume(
                thread_id=admission.thread_id, command_id="resume"
            )
            assert result.status == "completed" and effects == [
                ("once", leaf.idempotency_key)
            ]
            attempts = runtime.store.attempts(admission.thread_id)
            assert all(a.status == "succeeded" for a in attempts)
            assert len(client.registrations) == registrations
            parent = next(a for a in attempts if a.decision_id != leaf.decision_id)
            continuation = runtime.store.continuation(
                admission.thread_id, parent.tool_call_id
            )
            saved = await runtime.resources.checkpointer.aget_tuple(
                {
                    "configurable": {
                        "thread_id": admission.thread_id,
                        "checkpoint_ns": continuation.checkpoint_namespace,
                    }
                }
            )
            tool_messages = [
                m
                for m in saved.checkpoint["channel_values"]["messages"]
                if m.type == "tool"
            ]
            assert [(m.tool_call_id, m.name) for m in tool_messages] == [
                ("child-call", "effect")
            ]


@pytest.mark.asyncio
async def test_private_candidate_approved_child_resumes_while_same_child_sibling_stays_pending(
    tmp_path, monkeypatch
):
    import json

    candidate_selection(monkeypatch)
    client = NativeClient()
    client.state = "pending"
    monkeypatch.setattr(_owner, "Client", lambda *args, **kwargs: client)
    child_model = HistoryModel(
        files_by_description=True,
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "same-child-call",
                        "name": "write_file",
                        "args": {"file_path": "/unused", "content": "unused"},
                    }
                ],
            ),
            AIMessage(content="child done"),
        ],
    )
    children = (
        {
            "name": "worker",
            "description": "work",
            "system_prompt": "work",
            "model": child_model,
            "tools": (),
            "middleware": (),
            "skills": (),
        },
    )
    responses = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "sibling",
                    "name": "write_file",
                    "args": {"file_path": "/sibling.txt", "content": "keep"},
                },
                {
                    "id": "parent-A",
                    "name": "task",
                    "args": {"description": "A", "subagent_type": "worker"},
                },
                {
                    "id": "parent-B",
                    "name": "task",
                    "args": {"description": "B", "subagent_type": "worker"},
                },
            ],
        ),
        AIMessage(content="done"),
    ]
    async with open_advanced_durability(
        LocalDurability(directory=tmp_path, revision="candidate")
    ) as runtime:
        with runtime.store.acquire_deployment():
            deployment = runtime.store.initialize_enrollment(
                canonical(VERIFIED_IDENTITY), credential_binding("ctk_test")
            )
        async with candidate_agent(
            runtime, deployment, responses, client=client, children=children
        ) as agent:
            client.approval_names = {client.binding_names[("worker",), "write_file"]}
            await agent.abind_deployment()
            registrations = len(client.registrations)
            with runtime.store.acquire_deployment():
                admission = runtime.store.admit(
                    kind="start", command_id="start", message="go"
                )
            paused = await agent.ainvoke_durable(
                {"messages": [{"role": "user", "content": "go"}]},
                thread_id=admission.thread_id,
                command_id="start",
            )
            assert paused.status == "waiting" and len(paused.pending_decision_ids) == 2
            attempts = runtime.store.attempts(admission.thread_id)
            leaves = {
                json.loads(a.native_call_json)["args"]["content"]: a
                for a in attempts
                if a.status == "waiting"
            }
            assert set(leaves) == {"A", "B"}
            assert leaves["A"].tool_call_id != leaves["B"].tool_call_id
            config = {"configurable": {"thread_id": admission.thread_id}}
            before = await agent._owner._graph.aget_state(config)
            sibling = before.values["files"]["/sibling.txt"]
            client.approval_states = {leaves["A"].decision_id: "approved"}
            with runtime.store.acquire_deployment():
                runtime.store.admit(
                    kind="resume", command_id="resume-A", thread_id=admission.thread_id
                )
            partial = await agent.aresume(
                thread_id=admission.thread_id, command_id="resume-A"
            )
            assert partial.status == "waiting" and partial.pending_decision_ids == (
                leaves["B"].decision_id,
            )
            middle = await agent._owner._graph.aget_state(config)
            assert middle.values["files"]["/A.txt"]["content"] == "A"
            assert "/B.txt" not in middle.values["files"]
            assert middle.values["files"]["/sibling.txt"] == sibling
            client.approval_states[leaves["B"].decision_id] = "approved"
            with runtime.store.acquire_deployment():
                runtime.store.admit(
                    kind="resume", command_id="resume-B", thread_id=admission.thread_id
                )
            completed = await agent.aresume(
                thread_id=admission.thread_id, command_id="resume-B"
            )
            assert completed.status == "completed" and completed.output == ("done",)
            after = await agent._owner._graph.aget_state(config)
            assert after.values["files"]["/A.txt"] == middle.values["files"]["/A.txt"]
            assert after.values["files"]["/B.txt"]["content"] == "B"
            assert after.values["files"]["/sibling.txt"] == sibling
            final_attempts = runtime.store.attempts(admission.thread_id)
            assert len(final_attempts) == 5 and all(
                a.status == "succeeded" for a in final_attempts
            )
            assert {a.decision_id for a in final_attempts} == {
                a.decision_id for a in attempts
            }
            assert len(client.registrations) == registrations


@pytest.mark.asyncio
@pytest.mark.parametrize("close_during_failure", [False, True])
async def test_native_sibling_failure_retains_sync_handler_and_deployment_ownership(
    tmp_path, monkeypatch, close_during_failure
):
    import asyncio
    import threading
    from typing import Annotated

    from langchain_core.tools import InjectedToolArg, tool

    from openeight import ConfigurationError

    started, release = threading.Event(), threading.Event()
    failed = asyncio.Event()
    effects = []

    @tool
    def effect(value: str, idempotency_key: Annotated[str, InjectedToolArg]) -> str:
        """Apply one blocking destination mutation."""
        started.set()
        assert release.wait(10), "test did not release its admitted synchronous handler"
        effects.append((value, idempotency_key))
        return "receipt"

    class FailingSibling(HistoryModel):
        async def _agenerate(self, messages, *args, **kwargs):
            assert await asyncio.to_thread(started.wait, 5)
            failed.set()
            raise RuntimeError("sibling model failed")

    candidate_selection(monkeypatch)
    client = NativeClient()
    monkeypatch.setattr(_owner, "Client", lambda *args, **kwargs: client)
    child_a = HistoryModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "admitted", "name": "effect", "args": {"value": "once"}}
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "successor",
                        "name": "effect",
                        "args": {"value": "must not run"},
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    children = (
        {
            "name": "A",
            "description": "work",
            "system_prompt": "work",
            "model": child_a,
            "tools": (effect,),
            "middleware": (),
            "skills": (),
        },
        {
            "name": "B",
            "description": "work",
            "system_prompt": "work",
            "model": FailingSibling(responses=[AIMessage(content="unused")]),
            "tools": (),
            "middleware": (),
            "skills": (),
        },
    )
    responses = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "parent-A",
                    "name": "task",
                    "args": {"description": "A", "subagent_type": "A"},
                },
                {
                    "id": "parent-B",
                    "name": "task",
                    "args": {"description": "B", "subagent_type": "B"},
                },
            ],
        ),
        AIMessage(content="done"),
    ]
    async with open_advanced_durability(
        LocalDurability(directory=tmp_path, revision="candidate")
    ) as runtime:
        with runtime.store.acquire_deployment():
            deployment = runtime.store.initialize_enrollment(
                canonical(VERIFIED_IDENTITY), credential_binding("ctk_test")
            )
        async with candidate_agent(
            runtime, deployment, responses, client=client, children=children
        ) as agent:
            await agent.abind_deployment()
            with runtime.store.acquire_deployment():
                admission = runtime.store.admit(
                    kind="start", command_id="start", message="go"
                )
            execution = asyncio.create_task(
                agent.ainvoke_durable(
                    {"messages": [{"role": "user", "content": "go"}]},
                    thread_id=admission.thread_id,
                    command_id="start",
                )
            )
            try:
                await asyncio.wait_for(failed.wait(), 5)
                for _ in range(100):
                    if any(inv.native_cancelled for inv in agent._owner.roots):
                        break
                    await asyncio.sleep(0.01)
                assert any(inv.native_cancelled for inv in agent._owner.roots)
                assert not execution.done() and effects == []
                with pytest.raises(ConfigurationError) as busy:
                    with runtime.store.acquire_deployment():
                        pytest.fail(
                            "native failure released deployment while an admitted thread still ran"
                        )
                assert busy.value.reason == "integration_busy"
                if close_during_failure:
                    assert not (await agent.aclose(timeout=0.01)).closed
                    assert not execution.done()
                release.set()
                result = await asyncio.wait_for(
                    asyncio.gather(execution, return_exceptions=True), 5
                )
                assert isinstance(result[0], Exception)
                attempts = runtime.store.attempts(admission.thread_id)
                leaves = [a for a in attempts if a.idempotency_key is not None]
                assert len(leaves) == 1 and leaves[0].status == "succeeded"
                assert effects == [("once", leaves[0].idempotency_key)]
                assert leaves[0].terminal_event_json is not None
                assert any(
                    record.id == leaves[0].event_id
                    for record in runtime.store.pending(admission.thread_id)
                )
                assert (
                    runtime.store.load(admission.thread_id).status
                    == "reconciliation_required"
                )
            finally:
                release.set()
                await asyncio.gather(execution, return_exceptions=True)


@pytest.mark.asyncio
async def test_advanced_pre_root_admission_retains_factory_until_close_settles(
    tmp_path, monkeypatch
):
    import asyncio
    from contextlib import asynccontextmanager

    from openeight import ConfigurationError
    from openeight.deepagents import _admission

    candidate_selection(monkeypatch)
    client = NativeClient()
    monkeypatch.setattr(_owner, "Client", lambda *args, **kwargs: client)
    entered, release = asyncio.Event(), asyncio.Event()
    exited = []
    async with open_advanced_durability(
        LocalDurability(directory=tmp_path, revision="candidate")
    ) as runtime:
        with runtime.store.acquire_deployment():
            deployment = runtime.store.initialize_enrollment(
                canonical(VERIFIED_IDENTITY), credential_binding("ctk_test")
            )

        @asynccontextmanager
        async def resources():
            try:
                yield runtime.resources
            finally:
                exited.append("once")

        async with candidate_agent(
            runtime,
            deployment,
            [AIMessage(content="done")],
            client=client,
            resources=resources,
        ) as agent:
            await agent.abind_deployment()
            with runtime.store.acquire_deployment():
                admission = runtime.store.admit(
                    kind="start", command_id="start", message="go"
                )
            original = _admission._validate_durable_admission

            async def paused(*args, **kwargs):
                entered.set()
                await release.wait()
                return await original(*args, **kwargs)

            monkeypatch.setattr(_admission, "_validate_durable_admission", paused)
            execution = asyncio.create_task(
                agent.ainvoke_durable(
                    {"messages": [{"role": "user", "content": "go"}]},
                    thread_id=admission.thread_id,
                    command_id="start",
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), 2)
                assert not agent._owner.roots and agent._operation_tasks
                assert not (await agent.aclose(timeout=0.01)).closed
                assert exited == [] and not execution.done()
                release.set()
                with pytest.raises(ConfigurationError) as stopped:
                    await execution
                assert stopped.value.reason == "integration_closed"
                assert (await agent.aclose()).closed and exited == ["once"]
            finally:
                release.set()
                await asyncio.gather(execution, return_exceptions=True)
