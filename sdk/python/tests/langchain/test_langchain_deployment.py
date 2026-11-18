"""Bound LangChain durability preserves native results and immutable configuration."""

import os
from uuid import uuid4

import pytest

if not os.environ.get("OPENEIGHT_COOKBOOK_ROOT"):
    pytest.skip(
        "Requires OPENEIGHT_COOKBOOK_ROOT cookbook qualification examples",
        allow_module_level=True,
    )

pytest.importorskip("langgraph.checkpoint.sqlite.aio")

from _external_control import ExternalControlPlane
from _langchain_deployment_application import application
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel
from test_framework_boundary import ToolModel

from openeight import ConfigurationError, _adapter_http
from openeight.langchain import (
    DurableApprovalConfig,
    ReconciliationResult,
    create_agent,
)

CONFIG = {
    "agent_name": "support",
    "endpoint": "https://control.example",
    "credential": "ctk_test",
}


class Answer(BaseModel):
    answer: str


class Provider(ToolModel):
    calls: int = 0

    async def _agenerate(self, *args, **kwargs):
        self.calls += 1
        return await super()._agenerate(*args, **kwargs)


def response(value="ready"):
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "Answer", "id": "schema-" + value, "args": {"answer": value}}
        ],
    )


async def reconcile(_):
    return ReconciliationResult("unknown")


async def classify(_):
    return "terminal"


@pytest.fixture
def service(monkeypatch):
    service = ExternalControlPlane()
    monkeypatch.setattr(_adapter_http, "DeadlineHTTPTransport", lambda: service)
    return service


@pytest.mark.asyncio
async def test_explicit_binding_is_idempotent_and_structured_replay_is_command_pinned(
    tmp_path, service
):
    store, binding = application(tmp_path)
    approval = DurableApprovalConfig(
        store, reconcile, classify, deployment_binding=binding
    )
    async with AsyncSqliteSaver.from_conn_string(
        str(tmp_path / "native.sqlite")
    ) as saver:
        agent = create_agent(
            ToolModel(responses=[response()]),
            openeight={**CONFIG, "durable": approval},
            response_format=ToolStrategy(Answer),
            checkpointer=saver,
        )
        async with agent:
            assert service.requests == []
            await agent.abind_deployment()
            with binding.acquire_deployment():
                original = binding.deployment()
                assert original is not None
            await agent.abind_deployment()
            with binding.acquire_deployment():
                assert binding.deployment() == original
                command, _ = store.admit(
                    {
                        "kind": "start",
                        "command_id": str(uuid4()),
                        "case_id": "case",
                        "message": "answer",
                    },
                    case_id="case",
                )
            result = await agent.ainvoke_durable(
                {"messages": [{"role": "user", "content": "answer"}]},
                thread_id=command.thread_id,
                command_id=command.id,
            )
            assert result.status == "completed" and result.has_structured_response
            assert result.structured_response == {"answer": "ready"}
            repeated = await agent.ainvoke_durable(
                {"messages": [{"role": "user", "content": "answer"}]},
                thread_id=command.thread_id,
                command_id=command.id,
            )
            assert repeated == result
        with pytest.raises(ConfigurationError):
            store.load(command.thread_id)
        count = len(service.requests)
        binding.revision = "changed"
        with pytest.raises(ConfigurationError) as caught:
            async with create_agent(
                ToolModel(responses=[AIMessage(content="must not execute")]),
                openeight={**CONFIG, "durable": approval},
                response_format=ToolStrategy(Answer),
                checkpointer=saver,
            ):
                pytest.fail("Changed deployment entered")
        assert caught.value.reason == "durable_deployment_mismatch"
        assert len(service.requests) == count


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", ["execution", "checkpoint"])
async def test_nonempty_legacy_storage_rejects_binding_before_publication(
    tmp_path, service, existing
):
    from langchain.agents import create_agent as native

    store, binding = application(tmp_path)
    approval = DurableApprovalConfig(
        store, reconcile, classify, deployment_binding=binding
    )
    async with AsyncSqliteSaver.from_conn_string(
        str(tmp_path / "native.sqlite")
    ) as saver:
        if existing == "execution":
            store.admit(
                {
                    "kind": "start",
                    "command_id": str(uuid4()),
                    "case_id": "legacy",
                    "message": "old work",
                },
                case_id="legacy",
            )
        else:
            await native(
                Provider(responses=[AIMessage(content="old")]), checkpointer=saver
            ).ainvoke(
                {"messages": [("user", "old work")]},
                {"configurable": {"thread_id": "legacy"}},
            )
        async with create_agent(
            Provider(responses=[response()]),
            openeight={**CONFIG, "durable": approval},
            response_format=ToolStrategy(Answer),
            checkpointer=saver,
        ) as agent:
            assert service.requests == []
            with pytest.raises(ConfigurationError) as caught:
                await agent.abind_deployment()
            assert caught.value.reason == "durable_deployment_mismatch"
            assert all(
                operation != "RegisterTools" for operation, _ in service.requests
            )
        with binding.acquire_deployment():
            assert binding.deployment() is None


@pytest.mark.asyncio
async def test_uncertain_binding_retry_preserves_record_and_timestamp(
    tmp_path, service, monkeypatch
):
    store, binding = application(tmp_path)
    approval = DurableApprovalConfig(
        store, reconcile, classify, deployment_binding=binding
    )
    bind = binding.bind_deployment
    lost = False

    def application_write(value):
        nonlocal lost
        bind(value)
        if not lost:
            lost = True
            raise RuntimeError("private storage acknowledgment failure")

    monkeypatch.setattr(binding, "bind_deployment", application_write)
    async with AsyncSqliteSaver.from_conn_string(
        str(tmp_path / "native.sqlite")
    ) as saver:
        async with create_agent(
            Provider(responses=[response()]),
            openeight={**CONFIG, "durable": approval},
            response_format=ToolStrategy(Answer),
            checkpointer=saver,
        ) as agent:
            with pytest.raises(ConfigurationError) as caught:
                await agent.abind_deployment()
            assert caught.value.__context__ is None
            assert all(
                operation != "RegisterTools" for operation, _ in service.requests
            )
            with binding.acquire_deployment():
                saved = binding.deployment()
            await agent.abind_deployment()
            with binding.acquire_deployment():
                assert binding.deployment() == saved


@pytest.mark.asyncio
async def test_changed_runtime_configuration_rejects_before_state_or_network_reads(
    tmp_path, service
):
    from langchain_core.runnables import ConfigurableField

    from openeight import ModelVariant, RuntimeModelConfig

    store, binding = application(tmp_path)
    approval = DurableApprovalConfig(
        store, reconcile, classify, deployment_binding=binding
    )

    def make(saver, delay):
        model = Provider(responses=[AIMessage(content="done")]).configurable_fields(
            sleep=ConfigurableField(id="delay")
        )
        runtime = RuntimeModelConfig(
            variants=(ModelVariant(name="production", configurable={"delay": delay}),)
        )
        return create_agent(
            model,
            openeight={**CONFIG, "durable": approval, "runtime_models": runtime},
            checkpointer=saver,
        )

    async with AsyncSqliteSaver.from_conn_string(
        str(tmp_path / "native.sqlite")
    ) as saver:
        async with make(saver, 0.01) as agent:
            await agent.abind_deployment()
        before = len(service.requests)
        with pytest.raises(ConfigurationError) as caught:
            async with make(saver, 0.02):
                pytest.fail("Changed measured configuration admitted")
        assert (
            caught.value.reason == "durable_deployment_mismatch"
            and len(service.requests) == before
        )
        async with make(saver, 0.01) as agent:
            await agent.avalidate_deployment()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record", ["thread", "command", "sibling_thread", "sibling_command", "wrong_type"]
)
async def test_foreign_application_record_rejects_before_provider(
    tmp_path, service, monkeypatch, record
):
    from dataclasses import replace

    store, binding = application(tmp_path)
    approval = DurableApprovalConfig(
        store, reconcile, classify, deployment_binding=binding
    )
    model = Provider(responses=[response()])
    async with AsyncSqliteSaver.from_conn_string(
        str(tmp_path / "native.sqlite")
    ) as saver:
        async with create_agent(
            model,
            openeight={**CONFIG, "durable": approval},
            response_format=ToolStrategy(Answer),
            checkpointer=saver,
        ) as agent:
            await agent.abind_deployment()
            with binding.acquire_deployment():
                command, _ = store.admit(
                    {
                        "kind": "start",
                        "command_id": str(uuid4()),
                        "case_id": "case",
                        "message": "answer",
                    },
                    case_id="case",
                )
            method = (
                "load"
                if record in {"thread", "sibling_thread", "wrong_type"}
                else "command"
            )
            read = getattr(store, method)

            def application_record(*args):
                value = read(*args)
                if record in {"sibling_thread", "sibling_command"}:
                    return replace(value, id=str(uuid4()))
                if record == "wrong_type":
                    return command
                return replace(value, deployment_id=str(uuid4()))

            monkeypatch.setattr(store, method, application_record)
            with pytest.raises(ConfigurationError) as caught:
                await agent.ainvoke_durable(
                    {"messages": [{"role": "user", "content": "answer"}]},
                    thread_id=command.thread_id,
                    command_id=command.id,
                )
            assert (
                caught.value.reason == "durable_deployment_mismatch"
                and model.calls == 0
            )


def test_released_application_ownership_context_cannot_reactivate(tmp_path):
    from contextvars import copy_context

    _, binding = application(tmp_path)
    with binding.acquire_deployment():
        previous = copy_context()
        previous.run(binding.require_owned)
    with binding.acquire_deployment():
        binding.require_owned()
        with pytest.raises(ConfigurationError):
            previous.run(binding.require_owned)


@pytest.mark.asyncio
async def test_later_turn_does_not_change_prior_structured_projection(
    tmp_path, service
):
    store, binding = application(tmp_path)
    turns = 0

    async def disposition(_):
        nonlocal turns
        turns += 1
        return "waiting_customer" if turns == 1 else "terminal"

    approval = DurableApprovalConfig(
        store, reconcile, disposition, deployment_binding=binding
    )
    model = Provider(responses=[response("first"), response("second")])
    async with AsyncSqliteSaver.from_conn_string(
        str(tmp_path / "native.sqlite")
    ) as saver:
        async with create_agent(
            model,
            openeight={**CONFIG, "durable": approval},
            response_format=ToolStrategy(Answer),
            checkpointer=saver,
        ) as agent:
            await agent.abind_deployment()
            with binding.acquire_deployment():
                first, _ = store.admit(
                    {
                        "kind": "start",
                        "command_id": str(uuid4()),
                        "case_id": "case",
                        "message": "first",
                    },
                    case_id="case",
                )
            original = await agent.ainvoke_durable(
                {"messages": [{"role": "user", "content": "first"}]},
                thread_id=first.thread_id,
                command_id=first.id,
            )
            assert (
                original.structured_response == {"answer": "first"}
                and original.status == "waiting_customer"
            )
            with binding.acquire_deployment():
                reply, _ = store.admit(
                    {
                        "kind": "reply",
                        "command_id": str(uuid4()),
                        "thread_id": first.thread_id,
                        "message": "second",
                    }
                )
            later = await agent.areply(
                "second", thread_id=first.thread_id, command_id=reply.id
            )
            assert (
                later.structured_response == {"answer": "second"}
                and later.status == "completed"
            )
            replayed = await agent.ainvoke_durable(
                {"messages": [{"role": "user", "content": "first"}]},
                thread_id=first.thread_id,
                command_id=first.id,
            )
            assert (
                replayed.has_structured_response
                and replayed.structured_response == original.structured_response
            )
            assert replayed.status == "completed" and model.calls == 2


@pytest.mark.asyncio
async def test_initial_binding_sanitizes_closed_real_saver_failure(tmp_path, service):
    store, binding = application(tmp_path)
    approval = DurableApprovalConfig(
        store, reconcile, classify, deployment_binding=binding
    )
    async with AsyncSqliteSaver.from_conn_string(
        str(tmp_path / "native.sqlite")
    ) as saver:
        async with create_agent(
            Provider(responses=[response()]),
            openeight={**CONFIG, "durable": approval},
            response_format=ToolStrategy(Answer),
            checkpointer=saver,
        ) as agent:
            await saver.conn.close()
            with pytest.raises(ConfigurationError) as caught:
                await agent.abind_deployment()
            assert caught.value.reason == "durable_deployment_mismatch"
            assert caught.value.__context__ is None and caught.value.__cause__ is None
            with binding.acquire_deployment():
                assert binding.deployment() is None


@pytest.mark.asyncio
async def test_live_readiness_uses_one_deadline_for_authentication_and_catalog(
    tmp_path, service, monkeypatch
):
    store, binding = application(tmp_path)
    approval = DurableApprovalConfig(
        store, reconcile, classify, deployment_binding=binding
    )
    async with AsyncSqliteSaver.from_conn_string(
        str(tmp_path / "native.sqlite")
    ) as saver:
        async with create_agent(
            Provider(responses=[response()]),
            openeight={**CONFIG, "durable": approval},
            response_format=ToolStrategy(Answer),
            checkpointer=saver,
        ) as agent:
            await agent.abind_deployment()
            await agent.aflush()
            deadlines = []
            send = service.send

            def external(request, deadline):
                if request.url.path.endswith("/GetToolReadiness"):
                    deadlines.append(deadline)
                return send(request, deadline)

            monkeypatch.setattr(service, "send", external)
            await agent.areadiness(0.5)
            assert (
                len(deadlines) == 2
                and deadlines[0] is not None
                and deadlines[0] == deadlines[1]
            )


@pytest.mark.asyncio
async def test_legacy_static_object_durability_remains_unbound(tmp_path, service):
    store, binding = application(tmp_path)
    command, _ = store.admit(
        {
            "kind": "start",
            "command_id": str(uuid4()),
            "case_id": "legacy",
            "message": "go",
        },
        case_id="legacy",
    )
    approval = DurableApprovalConfig(store, reconcile, classify)
    async with AsyncSqliteSaver.from_conn_string(
        str(tmp_path / "native.sqlite")
    ) as saver:
        async with create_agent(
            Provider(responses=[AIMessage(content="done")]),
            openeight={**CONFIG, "durable": approval},
            checkpointer=saver,
        ) as agent:
            result = await agent.ainvoke_durable(
                {"messages": [{"role": "user", "content": "go"}]},
                thread_id=command.thread_id,
                command_id=command.id,
            )
            assert result.status == "completed" and not result.has_structured_response
    assert not binding.has_enrollment()


@pytest.mark.asyncio
async def test_string_binding_uses_native_provider_and_rejects_changed_spec(
    tmp_path, service, monkeypatch
):
    pytest.importorskip("langchain_anthropic")

    from inspect import getclosurevars

    from langchain_anthropic import ChatAnthropic

    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-construction-only")
    store, binding = application(tmp_path)
    approval = DurableApprovalConfig(
        store, reconcile, classify, deployment_binding=binding
    )
    async with AsyncSqliteSaver.from_conn_string(
        str(tmp_path / "native.sqlite")
    ) as saver:
        async with create_agent(
            "anthropic:claude-sonnet-4-5",
            openeight={**CONFIG, "durable": approval},
            checkpointer=saver,
        ) as agent:
            await agent.abind_deployment()
            assert isinstance(
                getclosurevars(
                    agent._owner._graph.nodes["model"].bound.afunc
                ).nonlocals["model"],
                ChatAnthropic,
            )
            with binding.acquire_deployment():
                assert (
                    "offline-construction-only"
                    not in binding.deployment().manifest_json
                )
        count = len(service.requests)
        with pytest.raises(ConfigurationError) as caught:
            async with create_agent(
                "anthropic:claude-haiku-4-5",
                openeight={**CONFIG, "durable": approval},
                checkpointer=saver,
            ):
                pytest.fail("Changed model string admitted")
        assert (
            caught.value.reason == "durable_deployment_mismatch"
            and len(service.requests) == count
        )


@pytest.mark.asyncio
async def test_concurrent_async_commands_do_not_share_os_thread_ownership(
    tmp_path, service
):
    import asyncio

    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingProvider(Provider):
        async def _agenerate(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return await super()._agenerate(*args, **kwargs)

    store, binding = application(tmp_path)
    approval = DurableApprovalConfig(
        store, reconcile, classify, deployment_binding=binding
    )
    model = BlockingProvider(responses=[AIMessage(content="done")])
    async with AsyncSqliteSaver.from_conn_string(
        str(tmp_path / "native.sqlite")
    ) as saver:
        async with create_agent(
            model, openeight={**CONFIG, "durable": approval}, checkpointer=saver
        ) as agent:
            await agent.abind_deployment()
            with binding.acquire_deployment():
                command, _ = store.admit(
                    {
                        "kind": "start",
                        "command_id": str(uuid4()),
                        "case_id": "case",
                        "message": "go",
                    },
                    case_id="case",
                )
            value = {"messages": [{"role": "user", "content": "go"}]}
            first = asyncio.create_task(
                agent.ainvoke_durable(
                    value, thread_id=command.thread_id, command_id=command.id
                )
            )
            await asyncio.wait_for(entered.wait(), 3)
            try:
                with pytest.raises(ConfigurationError) as caught:
                    await agent.ainvoke_durable(
                        value, thread_id=command.thread_id, command_id=command.id
                    )
                assert caught.value.reason == "durable_deployment_busy"
            finally:
                release.set()
            assert (await first).status == "completed" and model.calls == 1


@pytest.mark.asyncio
async def test_bound_handler_can_invoke_distinct_legacy_owner_without_scope_leak(
    tmp_path, service
):
    from typing import Annotated

    from langchain_core.tools import InjectedToolArg, tool

    outer_directory, inner_directory = tmp_path / "outer", tmp_path / "inner"
    outer_directory.mkdir(mode=0o700)
    inner_directory.mkdir(mode=0o700)
    outer_store, binding = application(outer_directory)
    inner_store, _ = application(inner_directory)
    inner_command, _ = inner_store.admit(
        {
            "kind": "start",
            "command_id": str(uuid4()),
            "case_id": "inner",
            "message": "inner",
        },
        case_id="inner",
    )
    outer_approval = DurableApprovalConfig(
        outer_store, reconcile, classify, deployment_binding=binding
    )
    inner_approval = DurableApprovalConfig(inner_store, reconcile, classify)
    effects = []
    async with AsyncSqliteSaver.from_conn_string(
        str(inner_directory / "native.sqlite")
    ) as inner_saver:
        async with create_agent(
            Provider(responses=[AIMessage(content="inner done")]),
            openeight={**CONFIG, "durable": inner_approval},
            checkpointer=inner_saver,
        ) as inner:

            @tool
            async def write_todos(
                value: str, idempotency_key: Annotated[str, InjectedToolArg]
            ) -> str:
                """Invoke another application-owned durable agent."""
                result = await inner.ainvoke_durable(
                    {"messages": [{"role": "user", "content": "inner"}]},
                    thread_id=inner_command.thread_id,
                    command_id=inner_command.id,
                )
                effects.append(result.status)
                return value

            model = Provider(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "nested",
                                "name": "write_todos",
                                "args": {"value": "outer"},
                            }
                        ],
                    ),
                    AIMessage(content="outer done"),
                ]
            )
            async with AsyncSqliteSaver.from_conn_string(
                str(outer_directory / "native.sqlite")
            ) as outer_saver:
                async with create_agent(
                    model,
                    [write_todos],
                    openeight={**CONFIG, "durable": outer_approval},
                    checkpointer=outer_saver,
                ) as outer:
                    await outer.abind_deployment()
                    with binding.acquire_deployment():
                        command, _ = outer_store.admit(
                            {
                                "kind": "start",
                                "command_id": str(uuid4()),
                                "case_id": "outer",
                                "message": "outer",
                            },
                            case_id="outer",
                        )
                    result = await outer.ainvoke_durable(
                        {"messages": [{"role": "user", "content": "outer"}]},
                        thread_id=command.thread_id,
                        command_id=command.id,
                    )
                    assert result.status == "completed" and effects == ["completed"]
