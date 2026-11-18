"""Consumer contracts: checked statically, never executed against a service."""

from collections.abc import AsyncGenerator
from typing import Any, assert_type

from openeight import (
    AgentReadiness,
    Client,
    CompatibilityReport,
    DeliveryStatus,
    GovernedRun,
    LifecycleStatus,
    OpenEightConfig,
    RegistrationResult,
    ToolRegistration,
)
from openeight.deepagents.durable import DurableThreadStatus
from openeight.langchain import create_agent
from openeight.langchain.durable import DurableAgentResult
from openeight.managed import (
    CatalogReadiness,
    CatalogSnapshot,
    CatalogStatus,
    DurableAgent,
    LocalDurableAgent,
    ManagedAgent,
    ManagedStream,
)


def ordinary(agent: ManagedAgent, client: Client) -> None:
    assert_type(agent.invoke({"messages": []}), Any)
    assert_type(agent.batch([{"messages": []}]), list[Any])
    assert_type(agent.stream({"messages": []}), ManagedStream)
    assert_type(agent.astream({"messages": []}), AsyncGenerator[Any, None])
    assert_type(agent.validate(), CompatibilityReport)
    assert_type(agent.publish(), CatalogSnapshot)
    assert_type(agent.readiness(), CatalogReadiness)
    assert_type(agent.catalog_status(), CatalogStatus)
    assert_type(agent.lifecycle_status(), LifecycleStatus)
    assert_type(agent.flush(), DeliveryStatus)
    assert_type(agent.close(), DeliveryStatus)
    assert_type(client.governed_run(), GovernedRun)
    assert_type(agent.catalog_snapshot().facts, tuple[ToolRegistration, ...])
    assert_type(agent.catalog_snapshot().results, tuple[RegistrationResult, ...])
    assert_type(agent.readiness().readiness, AgentReadiness)
    assert_type(agent.readiness().unpublished, tuple[str, ...])
    with agent as opened:
        assert_type(opened, ManagedAgent)
    agent.close(timeout="slow")  # type: ignore[arg-type]
    agent.release_thread(123)  # type: ignore[arg-type]
    agent.stream({}, stream_mode="not-a-mode")  # type: ignore[arg-type]


async def asynchronous(
    agent: ManagedAgent, durable: DurableAgent, local: LocalDurableAgent
) -> None:
    assert_type(await agent.areadiness(), CatalogReadiness)
    assert_type(await agent.aclose(), DeliveryStatus)
    assert_type(
        await durable.aresume(thread_id="thread", command_id="command"),
        DurableAgentResult,
    )
    assert_type(await local.astatus(thread_id="thread"), DurableThreadStatus)
    assert_type(
        await local.astart({"messages": []}, command_id="command"), DurableAgentResult
    )
    async with agent as opened:
        assert_type(opened, ManagedAgent)
    await durable.aresume(thread_id=1, command_id="command")  # type: ignore[arg-type]


def constructors(config: OpenEightConfig) -> None:
    assert_type(
        create_agent("provider:model", openeight=config), ManagedAgent | DurableAgent
    )
    create_agent(42, openeight=config)  # type: ignore[arg-type]
    create_agent("provider:model", openeight=config, tools=42)  # type: ignore[arg-type]
