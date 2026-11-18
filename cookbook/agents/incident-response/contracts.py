"""Immutable example-local execution and evidence records."""

from dataclasses import dataclass, field
from typing import Literal

Operation = Literal["restart_replica", "rollback_release", "set_replica_count"]
Mode = Literal["scripted", "provider"]
Role = Literal["coordinator", "diagnostics", "remediation", "reporting"]


@dataclass(frozen=True)
class ServiceSnapshot:
    service_id: str
    release: str
    replicas: int
    revision: int
    restart_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class Mutation:
    action_id: str
    service_id: str
    operation: Operation
    expected_revision: int
    arguments_json: str


@dataclass(frozen=True)
class EffectRecord:
    effect_id: str
    mutation: Mutation
    before_revision: int
    after_revision: int
    completed_at: str


@dataclass(frozen=True)
class ReadResult:
    service_id: str
    revision: int
    facts_json: str


@dataclass(frozen=True)
class ToolResult:
    value_json: str


@dataclass(frozen=True)
class Settings:
    endpoint: str
    agent_name: str
    credential: str = field(repr=False)
    mode: Mode = "scripted"
    provider_model: str | None = None
    provider_credential: str | None = field(default=None, repr=False)
    approval_timeout_seconds: float = 300.0
    capture_content: Literal["none", "inputs", "outputs", "all"] = "none"
    redaction: Literal["none", "synthetic-sentinels"] = "none"
    profile: Literal["default"] = "default"


@dataclass(frozen=True)
class CatalogEntry:
    child_path: tuple[str, ...]
    native_name: str
    catalog_name: str
    tool_id: str | None
    tool_version_id: str | None
    ready: bool | None


@dataclass(frozen=True)
class CatalogReport:
    generation: int
    publication_complete: bool
    entries: tuple[CatalogEntry, ...]
    error_code: str | None


@dataclass(frozen=True)
class EvidenceRef:
    run_id: str | None = None
    decision_id: str | None = None
    tool_id: str | None = None
    tool_version_id: str | None = None
    approval_id: str | None = None


@dataclass(frozen=True)
class ActionClaim:
    status: Literal["proposed", "completed"]
    mutation: Mutation
    effect_id: str | None


@dataclass(frozen=True)
class IncidentReport:
    schema_version: Literal[1]
    service_id: str
    observed_revision: int
    actions: tuple[ActionClaim, ...]


@dataclass(frozen=True)
class ReportValidation:
    status: Literal["passed", "failed", "not_produced"]
    artifact_path: str
    report: IncidentReport | None
    error_code: str | None


@dataclass(frozen=True)
class InvocationReport:
    status: Literal[
        "completed", "blocked", "approval_timeout", "business_rejected", "failed"
    ]
    error_code: str | None
    runner_summary: str
    report_validation: ReportValidation
    evidence: tuple[EvidenceRef, ...]


@dataclass(frozen=True)
class DeliveryReport:
    status: Literal["drained", "incomplete", "failed"]
    error_code: str | None


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Literal["passed", "failed", "pending"]
    detail: str


@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    scenario: str
    mode: Mode
    before: ServiceSnapshot
    after: ServiceSnapshot
    effects: tuple[EffectRecord, ...]
    invocation: InvocationReport
    delivery: DeliveryReport
    checks: tuple[CheckResult, ...]
