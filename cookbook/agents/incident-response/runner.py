"""One fresh incident per entered managed owner; never retry uncertain effects."""

from uuid import uuid4
from agent import open_incident_agent
from content import build_configuration
from contracts import CatalogReport, CheckResult, ScenarioResult, Settings
from models import build_models
from simulator import IncidentSimulator, canonical, initial_snapshot


def new_simulator():
    return IncidentSimulator(initial=initial_snapshot(), known_releases=("v41", "v42"))


async def run_catalog(*, settings: Settings) -> CatalogReport:
    models = build_models(settings=settings, scenario="investigate")
    async with open_incident_agent(
        models=models,
        simulator=new_simulator(),
        openeight=build_configuration(settings=settings),
        profile=settings.profile,
    ) as session:
        return await session.catalog()


async def run_scenario(*, name: str, settings: Settings) -> ScenarioResult:
    models = build_models(settings=settings, scenario=name)
    simulator = new_simulator()
    before = simulator.snapshot()
    async with open_incident_agent(
        models=models,
        simulator=simulator,
        openeight=build_configuration(settings=settings),
        profile=settings.profile,
    ) as session:
        invocation = await session.invoke(
            prompt=canonical({"service_id": before.service_id, "scenario": name})
        )
        delivery = await session.flush(timeout_seconds=30)
        report = invocation.report_validation
        checks = [
            scenario_check(name, simulator, invocation),
            CheckResult(
                "report_artifact",
                "passed" if report.status == "passed" else "failed",
                report.error_code
                or "Child result and saved artifact match all receipts.",
            ),
            CheckResult(
                "content_capture",
                "passed" if session.capture_failures == 0 else "failed",
                "Capture failure counter is zero."
                if session.capture_failures == 0
                else "Capture failures or unavailable counters.",
            ),
            CheckResult(
                "product_acceptance",
                "pending",
                "Authenticated HTTP, Console, provider usage and export evidence are separate lanes.",
            ),
        ]
        return ScenarioResult(
            str(uuid4()),
            name,
            settings.mode,
            before,
            simulator.snapshot(),
            simulator.effects(),
            invocation,
            delivery,
            tuple(checks),
        )


def scenario_check(name, simulator, invocation=None):
    effects, after = simulator.effects(), simulator.snapshot()
    expected = {
        "restart": ("restart_replica", {"replica_id": "replica-1"}),
        "rollback": ("rollback_release", {"target_release": "v41"}),
        "scale": ("set_replica_count", {"replicas": 3}),
    }
    if name in expected:
        operation, arguments = expected[name]
        passed = (
            len(effects) == 1
            and effects[0].mutation.operation == operation
            and effects[0].mutation.arguments_json == canonical(arguments)
            and after.revision == 1
        )
    elif name == "stale-revision":
        passed = (
            len(effects) == 1
            and after.revision == 1
            and invocation is not None
            and invocation.status == "business_rejected"
            and invocation.error_code == "stale_revision"
        )
    else:
        passed = not effects and after.revision == 0
    return CheckResult(
        "scenario_effects",
        "passed" if passed else "failed",
        "Requested scenario effects verified."
        if passed
        else "Scenario effects do not match.",
    )
