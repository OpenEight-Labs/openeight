"""Native strategies and command-pinned durable JSON projections."""

import asyncio
import json

import pytest
from _external_control import ExternalControlPlane
from langchain.agents.structured_output import (
    AutoStrategy,
    ProviderStrategy,
    ToolStrategy,
)
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from pydantic import BaseModel, RootModel, field_serializer
from test_framework_boundary import ToolModel

from openeight import ConfigurationError, _adapter_http
from openeight.langchain import create_agent

CONFIG = {
    "agent_name": "support",
    "endpoint": "https://control.example",
    "credential": "ctk_test",
}


class Answer(BaseModel):
    answer: str


class NullAnswer(RootModel[None]):
    pass


@pytest.mark.parametrize("durable", [False, True])
def test_unknown_schema_dialect_preserves_ordinary_result_and_rejects_durable(
    tmp_path, control, durable
):
    if durable:
        pytest.importorskip("langgraph.checkpoint.sqlite.aio")
        pytest.importorskip("deepagents")
        from openeight.deepagents import LocalDurability, create_deep_agent

    schema = {
        "title": "UnknownDialect",
        "$schema": "https://schemas.example/PRIVATE-SENTINEL",
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    message = AIMessage(
        content="",
        tool_calls=[
            {"id": "schema", "name": "UnknownDialect", "args": {"answer": 123}}
        ],
    )
    if not durable:
        result = create_agent(
            ToolModel(responses=[message]),
            openeight=CONFIG,
            response_format=ToolStrategy(schema),
        ).invoke({"messages": [("user", "answer")]})
        assert result["structured_response"] == {"answer": 123}
        return
    before = len(control.requests)
    directory = (tmp_path / "durable").resolve()
    with pytest.raises(ConfigurationError) as caught:
        create_deep_agent(
            ToolModel(responses=[message]),
            openeight=CONFIG,
            response_format=ToolStrategy(schema),
            durability=LocalDurability(directory=directory, revision="release-1"),
        )
    assert caught.value.reason == "unsupported_execution_method"
    assert not directory.exists() and len(control.requests) == before
    assert "PRIVATE-SENTINEL" not in json.dumps(control.requests)


@pytest.fixture
def control(monkeypatch):
    service = ExternalControlPlane()
    monkeypatch.setattr(_adapter_http, "DeadlineHTTPTransport", lambda: service)
    return service


def response(value="ready"):
    return AIMessage(
        content="",
        tool_calls=[{"name": "Answer", "id": "schema", "args": {"answer": value}}],
    )


@pytest.mark.parametrize(
    "factory",
    [
        ("langchain", "create_agent"),
        ("langchain", "observe"),
        ("deepagents", "create_deep_agent"),
        ("deepagents", "observe"),
    ],
    indirect=True,
)
@pytest.mark.parametrize("strategy", ["tool", "provider", "auto", "schema"])
def test_native_ordinary_strategies_return_original_validated_objects(
    control, factory, strategy
):
    formats = {
        "tool": ToolStrategy(Answer),
        "provider": ProviderStrategy(Answer),
        "auto": AutoStrategy(Answer),
        "schema": Answer,
    }
    messages = (
        [AIMessage(content='{"answer":"ready"}')]
        if strategy == "provider"
        else [response()]
    )
    agent = factory(
        ToolModel(responses=messages),
        openeight=CONFIG,
        response_format=formats[strategy],
    )
    result = agent.invoke({"messages": [("user", "answer")]})
    assert result["structured_response"] == Answer(answer="ready")
    assert not control.decisions
    assert all(
        not fact.name.endswith("Answer") for fact in agent.catalog_snapshot().facts
    )


def test_synthetic_schema_collision_rejects_before_publication(control):
    @tool("Answer")
    def business(answer: str) -> str:
        """A real business operation."""
        pytest.fail("Schema collision executed a handler")

    async def run():
        agent = create_agent(
            ToolModel(responses=[response()]),
            [business],
            openeight=CONFIG,
            response_format=ToolStrategy(Answer),
        )
        with pytest.raises(ConfigurationError) as caught:
            await agent.apublish()
        assert caught.value.reason == "conflicting_tool_definitions"
        assert not control.requests

    asyncio.run(run())


def test_native_validation_retry_is_correlated_and_private(control):
    async def run():
        agent = create_agent(
            ToolModel(responses=[response(123), response()]),
            openeight=CONFIG,
            response_format=ToolStrategy(Answer),
        )
        result = await agent.ainvoke({"messages": [("user", "PRIVATE-INPUT")]})
        assert result["structured_response"].answer == "ready"
        events = [
            event
            for operation, body in control.requests
            if operation == "WriteTelemetry"
            for event in body.get("events", [])
        ]
        assert (
            len([event for event in events if event["observation_kind"] == "llm_call"])
            == 2
        )
        assert len({event["run_id"] for event in events}) == 1
        assert "PRIVATE-INPUT" not in json.dumps(control.requests)

    asyncio.run(run())


def test_unhandled_native_validation_error_is_sanitized(control):
    async def run():
        agent = create_agent(
            ToolModel(responses=[response({"PRIVATE": "INVALID"})]),
            openeight=CONFIG,
            response_format=ToolStrategy(Answer, handle_errors=False),
        )
        with pytest.raises(ConfigurationError) as caught:
            await agent.ainvoke({"messages": [("user", "answer")]})
        assert caught.value.reason == "structured_output_validation_failed"
        assert caught.value.__context__ is None and caught.value.__cause__ is None
        assert "PRIVATE" not in str(caught.value)

    asyncio.run(run())


def test_ordinary_raw_dict_preserves_native_no_validation_behavior(control):
    schema = {
        "title": "Answer",
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    agent = create_agent(
        ToolModel(responses=[response(123)]),
        openeight=CONFIG,
        response_format=ToolStrategy(schema),
    )
    assert agent.invoke({"messages": [("user", "answer")]})["structured_response"] == {
        "answer": 123
    }


def test_durable_structured_response_replays_exact_terminal_checkpoint(
    tmp_path, control
):
    pytest.importorskip("langgraph.checkpoint.sqlite.aio")

    pytest.importorskip("deepagents")
    from openeight.deepagents import LocalDurability, create_deep_agent

    async def run():
        provider = ToolModel(responses=[response()])
        agent = create_deep_agent(
            provider,
            openeight=CONFIG,
            response_format=ToolStrategy(Answer),
            catalog_naming="qualified-v1",
            durability=LocalDurability(
                directory=(tmp_path / "state").resolve(), revision="structured-test"
            ),
        )
        async with agent:
            await agent.ainitialize()
            first = await agent.astart(
                {"messages": [{"role": "user", "content": "answer"}]},
                command_id="answer",
            )
            assert first.has_structured_response and first.structured_response == {
                "answer": "ready"
            }
            calls = len(
                [
                    event
                    for operation, body in control.requests
                    if operation == "WriteTelemetry"
                    for event in body.get("events", [])
                    if event["observation_kind"] == "llm_call"
                ]
            )
            repeated = await agent.astart(
                {"messages": [{"role": "user", "content": "answer"}]},
                command_id="answer",
            )
            assert repeated == first
            assert (
                len(
                    [
                        event
                        for operation, body in control.requests
                        if operation == "WriteTelemetry"
                        for event in body.get("events", [])
                        if event["observation_kind"] == "llm_call"
                    ]
                )
                == calls
            )

    asyncio.run(run())


def test_failed_durable_projection_is_terminal_and_never_reexecutes(tmp_path, control):
    pytest.importorskip("langgraph.checkpoint.sqlite.aio")

    pytest.importorskip("deepagents")
    from openeight.deepagents import LocalDurability, create_deep_agent

    schema = {
        "title": "Answer",
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }

    async def run():
        agent = create_deep_agent(
            ToolModel(responses=[response(123)]),
            openeight=CONFIG,
            response_format=ToolStrategy(schema),
            catalog_naming="qualified-v1",
            durability=LocalDurability(
                directory=(tmp_path / "state").resolve(), revision="projection-test"
            ),
        )
        async with agent:
            await agent.ainitialize()
            input = {"messages": [{"role": "user", "content": "answer"}]}
            with pytest.raises(ConfigurationError) as caught:
                await agent.astart(input, command_id="answer")
            assert caught.value.reason == "structured_output_projection_failed"
            store = agent._local_runtime.store
            with store.acquire_deployment():
                admission = store.admit(
                    kind="start", command_id="answer", thread_id=None, message="answer"
                )
            command = store.command(admission.thread_id, "answer")
            assert command.status == "completed" and command.output_checkpoint_id
            count = sum(
                operation == "WriteTelemetry" for operation, _ in control.requests
            )
            with pytest.raises(ConfigurationError) as repeated:
                await agent.astart(input, command_id="answer")
            assert repeated.value.reason == "structured_output_projection_failed"
            assert store.command(admission.thread_id, "answer") == command
            assert (
                sum(operation == "WriteTelemetry" for operation, _ in control.requests)
                == count
            )

    asyncio.run(run())


def test_durable_json_null_has_explicit_presence_flag(tmp_path, control):
    pytest.importorskip("langgraph.checkpoint.sqlite.aio")

    pytest.importorskip("deepagents")
    from openeight.deepagents import LocalDurability, create_deep_agent

    async def run():
        agent = create_deep_agent(
            ToolModel(responses=[AIMessage(content="null")]),
            openeight=CONFIG,
            response_format=ProviderStrategy(NullAnswer),
            catalog_naming="qualified-v1",
            durability=LocalDurability(
                directory=(tmp_path / "state").resolve(), revision="null-test"
            ),
        )
        async with agent:
            await agent.ainitialize()
            result = await agent.astart(
                {"messages": [{"role": "user", "content": "answer"}]},
                command_id="answer",
            )
            assert result.has_structured_response and result.structured_response is None

    asyncio.run(run())


def test_custom_serializers_reject_durable_before_publication(tmp_path, control):
    pytest.importorskip("langgraph.checkpoint.sqlite.aio")

    pytest.importorskip("deepagents")
    from openeight.deepagents import LocalDurability, create_deep_agent

    class Dynamic(BaseModel):
        answer: str

        @field_serializer("answer")
        def dynamic(self, value):
            pytest.fail("Custom serializer must not execute during qualification")

    class Nested(BaseModel):
        child: Dynamic

    with pytest.raises(ConfigurationError) as caught:
        create_deep_agent(
            ToolModel(responses=[response()]),
            openeight=CONFIG,
            response_format=ToolStrategy(Nested),
            durability=LocalDurability(
                directory=(tmp_path / "state").resolve(), revision="serializer-test"
            ),
        )
    assert caught.value.reason == "unsupported_execution_method"
    assert not control.requests


def test_full_raw_schema_constraints_apply_to_durable_projection():
    from openeight._framework._structured_output import (
        inspect_response_format,
        project_structured_response,
        validate_durable_response_format,
    )

    schema = {
        "title": "Result",
        "oneOf": [
            {
                "title": "Text",
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
            {"title": "Anything", "type": "object"},
        ],
    }
    spec = inspect_response_format(ToolStrategy(schema))
    validate_durable_response_format(spec)
    with pytest.raises(ConfigurationError) as caught:
        project_structured_response(spec, {"value": "text"})
    assert caught.value.reason == "structured_output_projection_failed"


@pytest.mark.parametrize("operation", ["invoke", "stream", "durable"])
def test_error_formatter_failure_is_sanitized_across_execution_modes(
    tmp_path, control, operation
):
    if operation == "durable":
        pytest.importorskip("deepagents")
        pytest.importorskip("langgraph.checkpoint.sqlite.aio")
        from openeight.deepagents import LocalDurability, create_deep_agent

    def formatter(error):
        raise RuntimeError("PRIVATE-SENTINEL " + str(error))

    async def run():
        options = {
            "openeight": CONFIG,
            "response_format": ToolStrategy(Answer, handle_errors=formatter),
        }
        provider = ToolModel(responses=[response(123)])
        agent = (
            create_deep_agent(
                provider,
                **options,
                catalog_naming="qualified-v1",
                durability=LocalDurability(
                    directory=(tmp_path / "state").resolve(), revision="formatter-test"
                ),
            )
            if operation == "durable"
            else create_agent(provider, **options)
        )
        async with agent:
            if operation == "durable":
                await agent.ainitialize()
            with pytest.raises(ConfigurationError) as caught:
                if operation == "invoke":
                    await agent.ainvoke({"messages": [("user", "answer")]})
                elif operation == "stream":
                    async for _ in agent.astream({"messages": [("user", "answer")]}):
                        pass
                else:
                    await agent.astart(
                        {"messages": [{"role": "user", "content": "answer"}]},
                        command_id="answer",
                    )
            assert caught.value.reason == "structured_output_validation_failed"
            assert caught.value.__cause__ is None and caught.value.__context__ is None
            assert "PRIVATE-SENTINEL" not in str(caught.value)
        assert "PRIVATE-SENTINEL" not in json.dumps(control.requests)

    asyncio.run(run())


@pytest.fixture
def factory(request):
    from importlib import import_module

    integration, method = request.param
    pytest.importorskip(integration)
    return getattr(import_module("openeight." + integration), method)
