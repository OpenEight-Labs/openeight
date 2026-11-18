"""Managed SDK composition and safe projections of its public results."""

from contextlib import asynccontextmanager
from typing import AsyncIterator, Protocol
from deepagents.backends import StateBackend
from langchain.agents.middleware import TodoListMiddleware
from openeight import ApprovalTimeout, BlockedError, OpenEightConfig
from openeight.deepagents import create_deep_agent
from contracts import (
    CatalogEntry,
    CatalogReport,
    DeliveryReport,
    EvidenceRef,
    InvocationReport,
    ReportValidation,
)
from reporting import ARTIFACT, ReportError, extract_report, validate_report
from simulator import BusinessError, IncidentSimulator
from models import ModelSet
from tools import build_tools
from profiles import CHILD_DESCRIPTIONS, configuration

from prompts import REPORT_PROMPT, COORDINATOR_PROMPT, SPECIALIST_PROMPT


class IncidentSession(Protocol):
    capture_failures: int | None

    async def catalog(self) -> CatalogReport: ...
    async def invoke(self, *, prompt: str) -> InvocationReport: ...
    async def flush(self, *, timeout_seconds: float) -> DeliveryReport: ...


def project_delivery(status):
    try:
        failed = any(
            getattr(status, key)
            for key in (
                "failed_records",
                "dropped_records",
                "catalog_failed_tools",
                "catalog_dropped_tools",
            )
        )
        pending = any(
            getattr(status, key) for key in ("pending_records", "catalog_pending_tools")
        )
        complete = (
            not pending
            and status.discovery_completeness == "complete"
            and status.catalog_delivery_state == "acknowledged"
            and status.catalog_total_tools > 0
            and status.catalog_acknowledged_tools == status.catalog_total_tools
        )
        return DeliveryReport(
            "failed" if failed else "drained" if complete else "incomplete",
            "delivery_failed"
            if failed
            else None
            if complete
            else "delivery_incomplete",
        )
    except AttributeError:
        return DeliveryReport("failed", "delivery_status_unknown")


class _Session:
    def __init__(self, owner, simulator):
        self.owner = owner
        self.simulator = simulator
        self.capture_failures = None

    async def catalog(self) -> CatalogReport:
        await self.owner.aflush(timeout=30)
        snapshot = self.owner.catalog_snapshot()
        results = {
            r.tool.name: r for r in snapshot.results if r.tool and r.tool_version
        }
        error, readiness = None, {}
        try:
            current = await self.owner.areadiness()
            readiness = {r.tool_version_id: r.eligible for r in current.readiness.tools}
        except Exception:
            error = "readiness_unavailable"
        entries = []
        for binding in snapshot.bindings:
            result = results.get(binding["name"])
            version = result.tool_version.id if result else None
            entries.append(
                CatalogEntry(
                    tuple(binding["child_path"]),
                    binding["native_name"],
                    binding["name"],
                    result.tool.id if result else None,
                    version,
                    readiness.get(version),
                )
            )
        return CatalogReport(
            snapshot.generation, snapshot.publication_complete, tuple(entries), error
        )

    async def invoke(self, *, prompt: str) -> InvocationReport:
        before = self.simulator.snapshot()
        status, error, evidence = "completed", None, ()
        validation = ReportValidation(
            "not_produced", ARTIFACT, None, "report_unavailable_after_stop"
        )
        try:
            result = await self.owner.ainvoke(
                {
                    "messages": [("user", prompt)],
                    "files": {},
                },
                config={"recursion_limit": 200},
            )
            try:
                report = extract_report(graph_result=result)
                validation = validate_report(
                    report=report,
                    before=before,
                    after=self.simulator.snapshot(),
                    effects=self.simulator.effects(),
                )
            except ReportError as exc:
                validation = ReportValidation("failed", ARTIFACT, None, exc.code)
        except (ApprovalTimeout, BlockedError) as exc:
            status = (
                "approval_timeout" if isinstance(exc, ApprovalTimeout) else "blocked"
            )
            error = exc.state
            result = exc.result
            evidence = (
                EvidenceRef(
                    run_id=getattr(result, "run_id", None),
                    decision_id=getattr(result, "decision_id", None),
                    approval_id=getattr(result, "approval_id", None),
                ),
            )
        except BusinessError as exc:
            status, error = "business_rejected", exc.code
        except Exception:
            status, error = "failed", "execution_failed"
        after, effects = self.simulator.snapshot(), self.simulator.effects()
        summary = f"{len(effects)} recorded effects; revision {before.revision} to {after.revision}."
        return InvocationReport(status, error, summary, validation, evidence)

    async def flush(self, *, timeout_seconds: float) -> DeliveryReport:
        try:
            status = await self.owner.aflush(timeout=timeout_seconds)
            self.capture_failures = sum(status.content_capture_failures.values())
            return project_delivery(status)
        except Exception:
            return DeliveryReport("failed", "delivery_failed")


def create_incident_agent(
    *,
    models: ModelSet,
    simulator: IncidentSimulator,
    openeight: OpenEightConfig,
    profile: str = "default",
    durability=None,
):
    children = []
    for role, model in (
        ("diagnostics", models.diagnostics),
        ("remediation", models.remediation),
        ("reporting", models.reporting),
    ):
        if model is None:
            continue
        prompt = REPORT_PROMPT if role == "reporting" else SPECIALIST_PROMPT
        children.append(
            {
                "name": role,
                "description": CHILD_DESCRIPTIONS[role],
                "model": model,
                "tools": build_tools(
                    simulator=simulator,
                    role=role,
                    profile=profile,
                    durable=durability is not None,
                ),
                "system_prompt": prompt,
            }
        )
    return create_deep_agent(
        models.coordinator,
        tools=build_tools(
            simulator=simulator,
            role="coordinator",
            profile=profile,
            durable=durability is not None,
        ),
        openeight=openeight,
        backend=StateBackend() if durability is None else None,
        **({"durability": durability} if durability is not None else {}),
        skills=[],
        subagents=children,
        middleware=[TodoListMiddleware()],
        system_prompt=COORDINATOR_PROMPT,
        **configuration(profile),
    )


@asynccontextmanager
async def open_incident_agent(
    *,
    models: ModelSet,
    simulator: IncidentSimulator,
    openeight: OpenEightConfig,
    profile: str = "default",
) -> AsyncIterator[IncidentSession]:
    async with create_incident_agent(
        models=models, simulator=simulator, openeight=openeight, profile=profile
    ) as owner:
        yield _Session(owner, simulator)
