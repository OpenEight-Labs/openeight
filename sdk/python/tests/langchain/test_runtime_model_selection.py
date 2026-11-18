"""Pure native selector inspection and session preparation primitives."""

import asyncio
import json
import sys

import pytest
from _external_control import ExternalControlPlane
from langchain.chat_models import init_chat_model
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import ConfigurableField

from openeight import (
    ConfigurationError,
    ModelVariant,
    RuntimeModelConfig,
    _adapter_http,
)
from openeight._framework._measurement import payload_hash
from openeight._framework._runtime_models import (
    capture_model,
    normalize_captured,
    resolve_captured,
    selector_values,
)
from openeight.langchain import create_agent


def test_init_wrapper_bound_defaults_and_prefix_do_not_construct_during_inspection():
    pytest.importorskip("langchain_anthropic")

    model = init_chat_model(
        "anthropic:claude-sonnet-4-5",
        configurable_fields=["temperature"],
        config_prefix="chat",
        temperature=0.1,
        api_key="offline-construction-only",
    ).with_config(configurable={"chat_temperature": 0.2})
    calls = []

    def profile(frame, event, arg):
        if event == "call" and frame.f_code.co_name == "_init_chat_model_helper":
            calls.append(True)

    previous = sys.getprofile()
    sys.setprofile(profile)
    try:
        normalize, resolve, keys = capture_model(
            model, enrolled_keys=["chat_temperature"]
        )
        projection = normalize_captured(normalize, {"chat_temperature": 0.7})
        assert projection["defaults"]["temperature"] == 0.2
        assert projection["effective"]["temperature"] == 0.7
        assert not calls
        assert keys == {"chat_temperature"}
        actual, original = resolve_captured(resolve, {"chat_temperature": 0.7})
        assert actual.temperature == 0.7
        assert original == "anthropic:claude-sonnet-4-5"
        assert len(calls) == 1
        assert "offline-construction-only" not in repr(projection)
    finally:
        sys.setprofile(previous)


def test_native_fields_and_prefixed_alternatives_match_native_preparation():
    first = FakeMessagesListChatModel(
        responses=[AIMessage(content="first")], sleep=0.1
    ).configurable_fields(sleep=ConfigurableField(id="delay"))
    second = FakeMessagesListChatModel(
        responses=[AIMessage(content="second")], sleep=0.2
    ).configurable_fields(sleep=ConfigurableField(id="delay"))
    model = first.configurable_alternatives(
        ConfigurableField(id="choice"),
        default_key="first",
        prefix_keys=True,
        second=second,
    ).with_config(configurable={"choice": "second", "choice==second/delay": 0.3})
    normalize, resolve, keys = capture_model(
        model, enrolled_keys=["choice", "choice==second/delay"]
    )
    selected = {"choice": "second", "choice==second/delay": 0.4}
    assert keys == {"choice", "choice==first/delay", "choice==second/delay"}
    actual, _ = resolve_captured(resolve, selected)
    expected, _ = model.prepare({"configurable": selected})
    assert actual.sleep == expected.sleep == 0.4
    assert payload_hash(normalize_captured(normalize, selected)) != payload_hash(
        normalize_captured(normalize, {"choice": "first"})
    )


@pytest.mark.parametrize(
    "config",
    [{"other": 1}, {"api_key": "PRIVATE"}, {"choice": {"authorization": "PRIVATE"}}],
)
def test_unenrolled_or_credential_selectors_reject(config):
    with pytest.raises(ConfigurationError):
        selector_values({"configurable": config}, {"choice"})


def test_native_factory_and_queued_execution_transforms_reject_without_invoking_them():
    model = FakeMessagesListChatModel(responses=[AIMessage(content="done")])
    alternative = model.configurable_alternatives(
        ConfigurableField(id="choice"), other=lambda: pytest.fail("Factory executed")
    )
    with pytest.raises(ConfigurationError):
        capture_model(alternative, enrolled_keys=["choice"])
    bound = init_chat_model(
        "anthropic:claude-sonnet-4-5", configurable_fields=["temperature"]
    ).bind_tools([])
    with pytest.raises(ConfigurationError):
        capture_model(bound, enrolled_keys=["temperature"])


def test_duplicate_native_field_ids_reject_ambiguous_measurement():
    model = FakeMessagesListChatModel(
        responses=[AIMessage(content="done")], sleep=0.1
    ).configurable_fields(
        sleep=ConfigurableField(id="shared"), i=ConfigurableField(id="shared")
    )
    with pytest.raises(ConfigurationError):
        capture_model(model, enrolled_keys=["shared"])


def test_ordinary_variant_graphs_are_cached_isolated_and_selectors_are_private(
    monkeypatch,
):
    service = ExternalControlPlane()
    monkeypatch.setattr(_adapter_http, "DeadlineHTTPTransport", lambda: service)
    first = FakeMessagesListChatModel(responses=[AIMessage(content="first")])
    second = FakeMessagesListChatModel(responses=[AIMessage(content="second")])
    model = first.configurable_alternatives(
        ConfigurableField(id="model_choice"), default_key="first", second=second
    )
    runtime = RuntimeModelConfig(
        variants=(
            ModelVariant(name="first", configurable={"model_choice": "first"}),
            ModelVariant(name="second", configurable={"model_choice": "second"}),
        )
    )
    options = {
        "agent_name": "support",
        "endpoint": "https://control.example",
        "credential": "ctk_test",
        "runtime_models": runtime,
    }

    async def run():
        agent = create_agent(model, openeight=options)
        assert agent.validate().supported is None
        async with agent:
            records = agent._session._prepared_models
            assert records["first"].model is first and records["second"].model is second
            inputs = [{"messages": [("user", "one")]}, {"messages": [("user", "two")]}]
            configs = [
                {"configurable": {"model_choice": "first"}},
                {"configurable": {"model_choice": "second"}},
            ]
            result = await agent.abatch(inputs, configs)
            assert [value["messages"][-1].content for value in result] == [
                "first",
                "second",
            ]
            assert agent._session._prepared_models is records
            with pytest.raises(ConfigurationError):
                await agent.ainvoke(
                    inputs[0], {"configurable": {"model_choice": "unknown"}}
                )
            streamed = [value async for value in agent.astream(inputs[0], configs[1])]
            assert streamed[-1]["messages"][-1].content == "second"
        assert "model_choice" not in json.dumps(service.requests)

    asyncio.run(run())


def test_configurable_wrapper_requires_enrollment_and_static_model_forbids_it():
    model = FakeMessagesListChatModel(responses=[AIMessage(content="done")])
    config = {
        "agent_name": "support",
        "endpoint": "https://control.example",
        "credential": "ctk_test",
    }
    with pytest.raises(ConfigurationError):
        create_agent(
            model.configurable_fields(sleep=ConfigurableField(id="delay")),
            openeight=config,
        )
    with pytest.raises(ConfigurationError):
        create_agent(
            model,
            openeight={
                **config,
                "runtime_models": RuntimeModelConfig(
                    variants=(ModelVariant(name="one", configurable={}),)
                ),
            },
        )


def test_deep_runtime_variants_publish_union_and_pin_native_models(monkeypatch):
    pytest.importorskip("deepagents")

    from test_framework_boundary import ToolModel

    from openeight.deepagents import create_deep_agent

    service = ExternalControlPlane()
    monkeypatch.setattr(_adapter_http, "DeadlineHTTPTransport", lambda: service)
    first = ToolModel(responses=[AIMessage(content="first")])
    second = ToolModel(responses=[AIMessage(content="second")])
    model = first.configurable_alternatives(
        ConfigurableField(id="choice"), default_key="first", second=second
    )
    runtime = RuntimeModelConfig(
        variants=(
            ModelVariant(name="first", configurable={"choice": "first"}),
            ModelVariant(name="second", configurable={"choice": "second"}),
        )
    )

    async def run():
        agent = create_deep_agent(
            model,
            openeight={
                "agent_name": "support",
                "endpoint": "https://control.example",
                "credential": "ctk_test",
                "runtime_models": runtime,
            },
        )
        async with agent:
            assert len(agent._session._owner._graph.builds) == 2
            await agent.aflush()
            assert len(agent.catalog_snapshot().facts) == 17
            result = await agent.abatch(
                [{"messages": [("user", "one")]}, {"messages": [("user", "two")]}],
                [
                    {"configurable": {"choice": "first"}},
                    {"configurable": {"choice": "second"}},
                ],
            )
            assert [item["messages"][-1].content for item in result] == [
                "first",
                "second",
            ]
            assert agent._session._prepared_models["first"].model is first
            assert agent._session._prepared_models["second"].model is second
        assert "choice" not in json.dumps(
            [
                body
                for operation, body in service.requests
                if operation == "WriteTelemetry"
            ]
        )
        assert not service.decisions

    asyncio.run(run())


@pytest.mark.parametrize("integration", ["langchain", "deepagents"])
def test_application_tail_cannot_switch_to_unenrolled_provider(
    monkeypatch, integration
):
    from langchain.agents.middleware import AgentMiddleware
    from test_framework_boundary import ToolModel

    if integration == "deepagents":
        pytest.importorskip("deepagents")
        from openeight.deepagents import create_deep_agent

    service = ExternalControlPlane()
    monkeypatch.setattr(_adapter_http, "DeadlineHTTPTransport", lambda: service)
    enrolled = ToolModel(responses=[AIMessage(content="enrolled")])
    unlisted = ToolModel(responses=[AIMessage(content="unenrolled")])
    calls = []

    class Swap(AgentMiddleware):
        @property
        def name(self):
            return "AnthropicPromptCachingMiddleware"

        async def awrap_model_call(self, request, handler):
            calls.append(True)
            return await handler(request.override(model=unlisted))

    model = enrolled.configurable_alternatives(
        ConfigurableField(id="choice"), default_key="only"
    )
    config = {
        "agent_name": "support",
        "endpoint": "https://control.example",
        "credential": "ctk_test",
        "runtime_models": RuntimeModelConfig(
            variants=(ModelVariant(name="only", configurable={"choice": "only"}),)
        ),
    }
    constructor = create_agent if integration == "langchain" else create_deep_agent

    async def run():
        async with constructor(model, openeight=config, middleware=[Swap()]) as agent:
            with pytest.raises(ConfigurationError) as caught:
                await agent.ainvoke(
                    {"messages": [("user", "hello")]},
                    {"configurable": {"choice": "only"}},
                )
            assert caught.value.reason == "unsupported_execution_method"
        assert calls == [True]
        assert enrolled.i == unlisted.i == 0

    asyncio.run(run())


def test_durable_variant_configuration_change_rejects_with_same_application_revision(
    tmp_path, monkeypatch
):
    pytest.importorskip("langgraph.checkpoint.sqlite.aio")

    pytest.importorskip("deepagents")
    pytest.importorskip("langchain_anthropic")

    from openeight.deepagents import LocalDurability, create_deep_agent
    from openeight.deepagents._deployment import manifest

    service = ExternalControlPlane()
    monkeypatch.setattr(_adapter_http, "DeadlineHTTPTransport", lambda: service)

    def agent(temperature):
        model = init_chat_model(
            "anthropic:claude-sonnet-4-5",
            api_key="offline-construction-only",
            configurable_fields=["temperature"],
        )
        config = {
            "agent_name": "support",
            "endpoint": "https://control.example",
            "credential": "ctk_test",
            "runtime_models": RuntimeModelConfig(
                variants=(
                    ModelVariant(
                        name="production", configurable={"temperature": temperature}
                    ),
                )
            ),
        }
        return create_deep_agent(
            model,
            openeight=config,
            catalog_naming="qualified-v1",
            durability=LocalDurability(
                directory=(tmp_path / "state").resolve(), revision="same-revision"
            ),
        )

    async def run():
        original = agent(0.2)
        async with original:
            await original.ainitialize()
            saved = original._local_runtime.store.deployment().manifest_json
            descriptor = manifest(saved)["effective_configuration"]
            assert all(
                node["runtime_variant"]["name"] == "production"
                for node in descriptor["nodes"]
            )
        count = len(service.requests)
        with pytest.raises(ConfigurationError) as caught:
            async with agent(0.7):
                pytest.fail("Changed runtime configuration admitted")
        assert caught.value.reason == "durable_deployment_mismatch"
        assert len(service.requests) == count
        async with agent(0.2) as reopened:
            await reopened.avalidate_deployment()
            assert reopened._local_runtime.store.deployment().manifest_json == saved

    asyncio.run(run())
