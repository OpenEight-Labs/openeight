"""Native tools; authorization is entirely owned by the managed SDK."""

from dataclasses import asdict
from langchain_core.tools import BaseTool, tool
from pydantic import StrictInt
from openeight.content import JSONValue
from contracts import Mutation, Role
from simulator import IncidentSimulator, canonical


def build_tools(
    *,
    simulator: IncidentSimulator,
    role: Role,
    profile: str = "default",
    durable: bool = False,
) -> tuple[BaseTool, ...]:
    @tool
    async def read_logs(service_id: str) -> dict[str, JSONValue]:
        """Read synthetic service logs and current revision without changing state."""
        return asdict(simulator.read_logs(service_id=service_id))

    @tool
    async def read_metrics(service_id: str) -> dict[str, JSONValue]:
        """Read synthetic error metrics and current revision without changing state."""
        return asdict(simulator.read_metrics(service_id=service_id))

    @tool
    async def read_deployments(service_id: str) -> dict[str, JSONValue]:
        """Read current and available releases and revision without changing state."""
        return asdict(simulator.read_deployments(service_id=service_id))

    @tool
    async def restart_replica(
        service_id: str, replica_id: str, expected_revision: StrictInt, action_id: str
    ) -> dict[str, JSONValue]:
        """Restart one synthetic replica at the expected revision; changes service state."""
        return asdict(
            simulator.apply(
                Mutation(
                    action_id,
                    service_id,
                    "restart_replica",
                    expected_revision,
                    canonical({"replica_id": replica_id}),
                )
            )
        )

    @tool
    async def rollback_release(
        service_id: str,
        target_release: str,
        expected_revision: StrictInt,
        action_id: str,
    ) -> dict[str, JSONValue]:
        """Change the synthetic service to a known release at the expected revision."""
        return asdict(
            simulator.apply(
                Mutation(
                    action_id,
                    service_id,
                    "rollback_release",
                    expected_revision,
                    canonical({"target_release": target_release}),
                )
            )
        )

    @tool
    async def set_replica_count(
        service_id: str,
        replicas: StrictInt,
        expected_revision: StrictInt,
        action_id: str,
    ) -> dict[str, JSONValue]:
        """Change synthetic replica membership at the expected revision."""
        return asdict(
            simulator.apply(
                Mutation(
                    action_id,
                    service_id,
                    "set_replica_count",
                    expected_revision,
                    canonical({"replicas": replicas}),
                )
            )
        )

    from profiles import DEFAULT

    if profile != "default":
        raise ValueError("unsupported_profile")
    path = () if role == "coordinator" else (role,)
    available = [read_logs, read_metrics, read_deployments]
    if role == "remediation":
        available += [restart_replica, rollback_release, set_replica_count]
    result = [item for item in available if item.name in DEFAULT[path]]
    if durable:
        from durable_tools import keyed_tools

        replacements = keyed_tools(simulator)
        result = [replacements.get(item.name, item) for item in result]
    return tuple(result)
