"""Effective durable configuration is measured from real assembly and saver state."""

import asyncio
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("deepagents")
pytest.importorskip("langgraph.checkpoint.sqlite.aio")

from _advanced_fixture import open_advanced_durability
from _external_control import ExternalControlPlane
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from openeight import ConfigurationError, _adapter_http
from openeight._framework._measurement import plain
from openeight.deepagents import (
    DeepAgentsDeploymentConfig,
    LocalDurability,
    create_deep_agent,
)
from openeight.deepagents._deployment import canonical, manifest
from openeight.deepagents._frameworks._measurement import compiled_middleware
from openeight.langchain.durable import DurableApprovalConfig

CONFIG = {
    "agent_name": "support",
    "endpoint": "https://control.example",
    "credential": "ctk_test",
}


def managed(directory):
    return create_deep_agent(
        FakeMessagesListChatModel(responses=[AIMessage(content="done")]),
        openeight=CONFIG,
        catalog_naming="qualified-v1",
        durability=LocalDurability(
            directory=Path(directory).resolve(), revision="measurement-test"
        ),
    )


@pytest.fixture
def control(monkeypatch):
    service = ExternalControlPlane()
    monkeypatch.setattr(_adapter_http, "DeadlineHTTPTransport", lambda: service)
    return service


def test_format_two_bind_reopen_and_live_mutation_before_external_reads(
    tmp_path, control
):
    async def run():
        first = managed(tmp_path / "state")
        async with first:
            await first.ainitialize()
            saved = first._local_runtime.store.deployment().manifest_json
            parsed = manifest(saved)
            assert parsed["format_version"] == 2
            assert parsed["effective_configuration"]["format_version"] == 2
            assert (
                parsed["effective_configuration_sha256"]
                == hashlib.sha256(
                    canonical(parsed["effective_configuration"]).encode()
                ).hexdigest()
            )
            count = len(control.requests)
            middleware = compiled_middleware(first._owner._graph)
            fs = next(
                item
                for item in middleware.values()
                if type(item).__name__ == "FilesystemMiddleware"
            )
            fs._grep_max_count += 1
            with pytest.raises(ConfigurationError) as caught:
                await first.avalidate_deployment()
            assert caught.value.reason == "durable_deployment_mismatch"
            assert len(control.requests) == count
            assert first._local_runtime.store.deployment().manifest_json == saved
        second = managed(tmp_path / "state")
        async with second:
            await second.avalidate_deployment()
            assert second._local_runtime.store.deployment().manifest_json == saved

    asyncio.run(run())


@pytest.mark.parametrize(
    "mutation", ["legacy", "missing", "unknown", "digest", "node", "edge", "runtime"]
)
def test_manifest_rejects_legacy_and_malformed_measurements(
    tmp_path, control, mutation
):
    async def run():
        agent = managed(tmp_path / "state")
        async with agent:
            await agent.ainitialize()
            original = manifest(agent._local_runtime.store.deployment().manifest_json)
        value = copy.deepcopy(original)
        if mutation == "legacy":
            value["format_version"] = 1
            value.pop("effective_configuration")
            value.pop("effective_configuration_sha256")
        elif mutation == "missing":
            value["effective_configuration"]["nodes"][0].pop("system_message_sha256")
        elif mutation == "unknown":
            value["effective_configuration"]["nodes"][0]["extra"] = True
        elif mutation == "digest":
            value["effective_configuration_sha256"] = "0" * 64
        elif mutation == "node":
            value["effective_configuration"]["nodes"].pop()
        elif mutation == "edge":
            next(
                node
                for node in value["effective_configuration"]["nodes"]
                if not node["path"]
            )["delegates_to"] = []
        elif mutation == "runtime":
            value["effective_configuration"]["nodes"][0]["runtime_variant"] = {
                "name": "one",
                "configuration_sha256": "a" * 64,
            }
        if mutation not in {"legacy", "digest"}:
            value["effective_configuration_sha256"] = hashlib.sha256(
                canonical(value["effective_configuration"]).encode()
            ).hexdigest()
        with pytest.raises(ConfigurationError) as caught:
            manifest(canonical(value))
        assert caught.value.reason == "durable_deployment_mismatch"
        assert manifest(canonical(original)) == original

    asyncio.run(run())


def test_measured_descriptor_is_immutable(tmp_path):
    async def run():
        agent = managed(tmp_path / "state")
        async with agent._open_local_resources() as resources:
            await agent._prepare(resources=resources, governance=agent._governance)
            build = agent._build(
                None, {}, resources=resources, governance=agent._governance
            )
            descriptor = build.composition["effective_configuration"]
            with pytest.raises(TypeError):
                descriptor["nodes"][0]["source"] = "nested"
            assert (
                next(node for node in plain(descriptor)["nodes"] if not node["path"])[
                    "source"
                ]
                == "explicit"
            )

    asyncio.run(run())


def test_advanced_admission_measures_configuration_independently_of_caller_digest(
    tmp_path, control
):
    from openeight.deepagents._deployment import credential_binding

    async def reconcile(attempt):
        pytest.fail("No effect reconciliation is needed for configuration inspection")

    async def classify_turn(command_id):
        return "terminal"

    async def run():
        async with open_advanced_durability(
            SimpleNamespace(directory=(tmp_path / "advanced").resolve())
        ) as runtime:
            identity = canonical(
                {
                    "kind": "verified_issuance",
                    "endpoint": CONFIG["endpoint"],
                    "agent_name": CONFIG["agent_name"],
                    **{
                        field: str(uuid4())
                        for field in (
                            "organization_id",
                            "workspace_id",
                            "agent_id",
                            "api_key_id",
                        )
                    },
                }
            )
            with runtime.store.acquire_deployment():
                deployment = runtime.store.initialize_enrollment(
                    identity, credential_binding(CONFIG["credential"])
                )
            approval = DurableApprovalConfig(runtime.store, reconcile, classify_turn)
            agent = create_deep_agent(
                FakeMessagesListChatModel(responses=[AIMessage(content="done")]),
                openeight={**CONFIG, "durable": approval},
                checkpointer=runtime.resources.checkpointer,
                catalog_naming="qualified-v1",
                durable_deployment=DeepAgentsDeploymentConfig(
                    deployment.deployment_id, "a" * 64, "b" * 64
                ),
            )
            async with agent:
                await agent.abind_deployment()
                saved = runtime.store.deployment().manifest_json
                count = len(control.requests)
                summary = next(
                    item
                    for item in compiled_middleware(agent._owner._graph).values()
                    if type(item).__name__ == "_DeepAgentsSummarizationMiddleware"
                )
                summary._lc_helper.summary_prompt += " changed"
                with pytest.raises(ConfigurationError) as caught:
                    await agent.avalidate_deployment()
                assert caught.value.reason == "durable_deployment_mismatch"
                assert len(control.requests) == count
                assert runtime.store.deployment().manifest_json == saved

    asyncio.run(run())


@pytest.mark.parametrize("attribute", ["_awrap_tool_call", "_wrap_tool_call"])
def test_removed_native_dispatch_gate_rejects_before_external_reads(
    tmp_path, control, attribute
):
    async def run():
        agent = managed(tmp_path / "state")
        async with agent:
            await agent.ainitialize()
            count = len(control.requests)
            setattr(agent._owner._graph.nodes["tools"].bound, attribute, None)
            with pytest.raises(ConfigurationError) as caught:
                await agent.avalidate_deployment()
            assert caught.value.reason == "durable_deployment_mismatch"
            assert len(control.requests) == count

    asyncio.run(run())


def test_native_todo_configuration_is_measured(tmp_path, control):
    from langchain.agents.middleware import TodoListMiddleware

    async def run():
        agent = create_deep_agent(
            FakeMessagesListChatModel(responses=[AIMessage(content="done")]),
            openeight=CONFIG,
            middleware=[TodoListMiddleware()],
            catalog_naming="qualified-v1",
            durability=LocalDurability(
                directory=(tmp_path / "state").resolve(), revision="todo-test"
            ),
        )
        async with agent:
            await agent.ainitialize()
            kinds = [
                entry["kind"]
                for node in agent._owner._composition["effective_configuration"][
                    "nodes"
                ]
                for entry in node["middleware"]
            ]
            assert any(kind.endswith(".TodoListMiddleware") for kind in kinds)
            await agent.avalidate_deployment()
            assert not any(
                operation == "WriteTelemetry" for operation, _ in control.requests
            )

    asyncio.run(run())
