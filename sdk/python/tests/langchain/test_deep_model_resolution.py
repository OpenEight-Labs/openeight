"""Native construction parity without provider or control-plane calls."""

import asyncio
import logging
from inspect import getclosurevars

import pytest

pytest.importorskip("deepagents")

from deepagents import create_deep_agent as native_deep
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, SystemMessage

from openeight import ConfigurationError
from openeight.deepagents import create_deep_agent
from openeight.deepagents._frameworks._inspection import native_graphs

CONFIG = {
    "agent_name": "support",
    "endpoint": "https://control.example",
    "credential": "ctk_test",
}


async def build(managed):
    session = managed._factory()
    await session._prepare(resources=session._resources, governance=session._governance)
    return session, session._build(None, {}, resources=session._resources)


def model_fields(graph):
    return getclosurevars(graph.nodes["model"].bound.func).nonlocals


@pytest.mark.parametrize(
    "prompt",
    [
        None,
        "Application prompt",
        SystemMessage(
            content=[{"type": "text", "text": "Application block"}], name="application"
        ),
    ],
)
def test_deep_object_root_children_prompts_and_descriptions_match_native(prompt):
    model = FakeMessagesListChatModel(responses=[AIMessage(content="done")])
    children = [
        {
            "name": "specialist",
            "description": "Specialist",
            "system_prompt": "Help",
            "tools": [],
        }
    ]
    native = native_deep(model, system_prompt=prompt, subagents=children)
    managed = create_deep_agent(
        model, system_prompt=prompt, subagents=children, openeight=CONFIG
    )

    async def run():
        session, built = await build(managed)
        expected = native_graphs(
            native, {(): None, ("general-purpose",): None, ("specialist",): None}
        )
        actual = native_graphs(
            built.graph, {control.child_path: None for control in built.controls}
        )
        assert set(actual) == set(expected)
        for path in actual:
            before, after = model_fields(expected[path]), model_fields(actual[path])
            assert before["model"] is after["model"] is model
            assert before["system_message"] == after["system_message"]

            def names(graph):
                return {
                    name: (
                        tool.description,
                        tool.get_input_schema().model_json_schema(),
                    )
                    for name, tool in graph.nodes["tools"].bound.tools_by_name.items()
                }

            assert names(expected[path]) == names(actual[path])
        assert built.graph.config["recursion_limit"] == 9999
        assert (
            model_fields(session._build(None, {}, resources=session._resources).graph)[
                "model"
            ]
            is model
        )

    asyncio.run(run())


def test_native_deep_recursion_default_survives_execution_and_explicit_limit(
    monkeypatch,
):
    from _external_control import ExternalControlPlane
    from langchain.agents.middleware import TodoListMiddleware
    from langgraph.errors import GraphRecursionError
    from test_framework_boundary import ToolModel

    from openeight import _adapter_http
    from openeight.deepagents import ToolSurface

    service = ExternalControlPlane()
    monkeypatch.setattr(_adapter_http, "DeadlineHTTPTransport", lambda: service)

    def candidate():
        responses = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "todo-" + str(index),
                        "name": "write_todos",
                        "args": {
                            "todos": [
                                {"content": "step " + str(index), "status": "completed"}
                            ]
                        },
                    }
                ],
            )
            for index in range(14)
        ]
        return create_deep_agent(
            ToolModel(responses=[*responses, AIMessage(content="finished")]),
            openeight=CONFIG,
            include_general_purpose=False,
            catalog_naming="qualified-v1",
            middleware=[TodoListMiddleware()],
            tool_surfaces=[ToolSurface((), ("write_todos",))],
        )

    async def run():
        result = await candidate().ainvoke({"messages": [("user", "run all steps")]})
        assert result["messages"][-1].content == "finished"
        assert len(service.decisions) == 14
        with pytest.raises(GraphRecursionError):
            await candidate().ainvoke(
                {"messages": [("user", "bounded")]}, {"recursion_limit": 25}
            )

    asyncio.run(run())


def test_deep_explicit_strings_resolve_independently_and_inherited_models_share(
    monkeypatch,
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-construction-only")
    managed = create_deep_agent(
        "anthropic:claude-sonnet-4-5",
        subagents=[
            {
                "name": "explicit",
                "description": "Independent",
                "model": "anthropic:claude-sonnet-4-5",
            },
            {"name": "inherited", "description": "Shared"},
        ],
        openeight=CONFIG,
    )
    assert managed.validate().supported is None

    async def run():
        session, built = await build(managed)
        graphs = native_graphs(
            built.graph, {control.child_path: None for control in built.controls}
        )
        models = {path: model_fields(graph)["model"] for path, graph in graphs.items()}
        assert models[()] is models[("inherited",)] is models[("general-purpose",)]
        assert models[("explicit",)] is not models[()]
        rebuilt = session._build(None, {}, resources=session._resources)
        assert model_fields(rebuilt.graph)["model"] is models[()]

    asyncio.run(run())


@pytest.mark.parametrize("factory", [create_deep_agent])
def test_invalid_model_resolution_is_sanitized_without_cause(factory):
    managed = factory("unknown-provider:SECRET-SENTINEL", openeight=CONFIG)

    async def run():
        session = managed._factory()
        with pytest.raises(ConfigurationError) as caught:
            await session._prepare(
                resources=session._resources, governance=session._governance
            )
        assert caught.value.__context__ is None
        assert caught.value.__cause__ is None
        assert "SECRET-SENTINEL" not in str(caught.value)

    asyncio.run(run())


def test_deep_resolution_rejects_native_diagnostic_leaks_without_changing_logging(
    monkeypatch, caplog
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-construction-only")
    name = "deepagents.profiles.harness.harness_profiles"
    caplog.set_level(logging.DEBUG, logger=name)
    level = logging.getLogger(name).level
    managed = create_deep_agent("anthropic:SECRET-SENTINEL", openeight=CONFIG)

    async def run():
        with pytest.raises(ConfigurationError) as caught:
            await build(managed)
        assert caught.value.reason == "unsafe_framework_diagnostics"

    asyncio.run(run())
    assert "SECRET-SENTINEL" not in caplog.text
    assert logging.getLogger(name).level == level


def test_native_cache_replacement_and_default_child_inheritance():
    from langchain_anthropic.middleware.prompt_caching import (
        AnthropicPromptCachingMiddleware,
    )

    model = FakeMessagesListChatModel(responses=[AIMessage(content="done")])
    caching = AnthropicPromptCachingMiddleware(
        ttl="1h", unsupported_model_behavior="ignore"
    )
    native = native_deep(model, middleware=[caching])
    managed = create_deep_agent(model, middleware=[caching], openeight=CONFIG)

    async def run():
        _, built = await build(managed)
        expected = native_graphs(native, {(): None, ("general-purpose",): None})
        actual = native_graphs(
            built.graph, {control.child_path: None for control in built.controls}
        )
        for path in expected:
            before, after = model_fields(expected[path]), model_fields(actual[path])
            assert before["system_message"] == after["system_message"]

            # Both graphs retain the application instance in their model wrapper.
            def objects(function):
                if getattr(function, "__self__", None) is not None:
                    return [function.__self__]
                if not hasattr(function, "__code__"):
                    return []
                return [
                    item
                    for value in getclosurevars(function).nonlocals.values()
                    if callable(value)
                    for item in objects(value)
                ]

            assert caching in objects(before["wrap_model_call_handler"])
            assert caching in objects(after["wrap_model_call_handler"])

    asyncio.run(run())
