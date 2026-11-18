"""Real Deep Agents and SDK execution against an offline HTTP control service."""

from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

import pytest
from openeight import (
    AgentReadiness,
    DecisionResult,
    FieldSelector,
    PolicyInputRequirement,
    RegistrationResult,
    Tool,
    ToolReadiness,
    ToolVersion,
)

from agent import open_incident_agent
from content import build_configuration
from contracts import Settings
from models import build_models
from runner import new_simulator, run_catalog, run_scenario
from tests.support.http_control import ControlHTTP


class Control:
    def __init__(self):
        self.rows = {}
        self.requests = []
        self.deny_tool = None

    def register_tools(self, facts):
        result = []
        for index, fact in enumerate(facts):
            write = "expected_revision" in fact.input_schema.get("properties", {})
            write = write or fact.name in {
                "coordinator.write_todos",
                "coordinator.task",
                "coordinator.write_file",
            }
            tool_id = str(uuid5(NAMESPACE_URL, fact.name))
            version_id = str(uuid5(NAMESPACE_URL, fact.name + "/v1"))
            row = RegistrationResult(
                index,
                "registered",
                "",
                Tool(
                    tool_id,
                    fact.name,
                    fact.description,
                    "write" if write else "read",
                    write,
                    None,
                    None,
                    "2026-11-15T00:00:00Z",
                ),
                ToolVersion(
                    version_id,
                    tool_id,
                    "1",
                    "sha256:" + "0" * 64,
                    fact.input_schema,
                    "sha256:" + "1" * 64 if write else None,
                    "2026-11-15T00:00:00Z",
                ),
                ToolReadiness(
                    index,
                    fact.name,
                    tool_id,
                    version_id,
                    "registered",
                    write,
                    True,
                    True,
                    "ready" if write else "read_only",
                ),
            )
            self.rows[version_id] = row
            result.append(row)
        return tuple(result)

    def get_tool_readiness(self, ids):
        rows = []
        for identity in ids or self.rows:
            result = self.rows[identity]
            row = replace(
                result.readiness,
                governance_revision_id=str(
                    uuid5(NAMESPACE_URL, identity + "/governance")
                ),
            )
            if row.governed:
                row = replace(
                    row,
                    policy_contract_set_digest="a" * 64,
                    policy_input_contracts=(
                        PolicyInputRequirement(
                            str(uuid5(NAMESPACE_URL, identity + "/policy")),
                            str(uuid5(NAMESPACE_URL, identity + "/policy-version")),
                            str(uuid5(NAMESPACE_URL, identity + "/binding")),
                            "b" * 64,
                            tuple(
                                FieldSelector(key)
                                for key in result.tool_version.input_schema[
                                    "properties"
                                ]
                            ),
                        ),
                    ),
                )
            rows.append(row)
        return AgentReadiness(tuple(rows), True, "registered", True, "ready")

    def decide(self, **request):
        self.requests.append(request)
        return DecisionResult(
            request["decision_id"],
            "deny" if request["tool_id"] == self.deny_tool else "allow",
            "",
            request["run_id"],
            False,
        )

    def write_telemetry(self, **body):
        pass

    @property
    def business_requests(self):
        return [
            request
            for request in self.requests
            if "expected_revision"
            in self.rows[request["tool_version_id"]].tool_version.input_schema.get(
                "properties", {}
            )
        ]


@pytest.fixture
def control():
    service = Control()
    with ControlHTTP(lambda: service):
        yield service


def settings():
    return Settings("http://localhost:7777", "incident-test", "ctk_test")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario,effects",
    [("investigate", 0), ("rollback", 1), ("restart", 1), ("scale", 1)],
)
async def test_scripted_specialists_report_actual_receipts(control, scenario, effects):
    result = await run_scenario(name=scenario, settings=settings())
    assert result.invocation.status == "completed"
    assert result.invocation.report_validation.status == "passed"
    assert len(result.effects) == effects
    assert result.delivery.status == "drained"
    assert len(control.business_requests) == effects


@pytest.mark.asyncio
async def test_publication_has_no_business_effect_or_decision(control):
    result = await run_catalog(settings=settings())
    assert result.publication_complete
    assert all(row.ready for row in result.entries)
    assert not control.requests


@pytest.mark.asyncio
async def test_stale_remediation_stops_without_repeating_the_effect(control):
    result = await run_scenario(name="stale-revision", settings=settings())
    assert result.invocation.status == "business_rejected"
    assert result.invocation.error_code == "stale_revision"
    assert result.invocation.report_validation.status == "not_produced"
    assert len(result.effects) == 1
    assert result.after.revision == 1
    assert len(control.business_requests) == 2


@pytest.mark.asyncio
async def test_denied_remediation_leaves_service_unchanged(control):
    config = settings()
    simulator = new_simulator()
    async with open_incident_agent(
        models=build_models(settings=config, scenario="rollback"),
        simulator=simulator,
        openeight=build_configuration(settings=config),
    ) as session:
        catalog = await session.catalog()
        target = next(
            row for row in catalog.entries if row.native_name == "rollback_release"
        )
        control.deny_tool = target.tool_id
        result = await session.invoke(
            prompt='{"service_id":"checkout","scenario":"rollback"}'
        )
        assert result.status == "blocked"
        assert simulator.effects() == ()
        assert len(control.business_requests) == 1
