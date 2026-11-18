"""Local compatibility reports; inspection never executes application factories."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, kw_only=True)
class CompatibilityIssue:
    code: str
    path: tuple[str, ...]
    severity: Literal["error", "warning", "deferred"]
    message: str


@dataclass(frozen=True, kw_only=True)
class CompatibilityReport:
    issues: tuple[CompatibilityIssue, ...]
    complete: bool
    supported: bool | None


def report(issues=()):
    issues = tuple(issues)
    complete = not any(issue.severity == "deferred" for issue in issues)
    supported = (
        False
        if any(issue.severity == "error" for issue in issues)
        else True
        if complete
        else None
    )
    return CompatibilityReport(issues=issues, complete=complete, supported=supported)


def inspect_inputs(
    *,
    model,
    governance,
    resources,
    checkpointer,
    middleware=(),
    state_schema=None,
    context_schema=None,
    checkpoints=None,
):
    """Inspect declarations without attribute delegation into native model factories."""
    issues = []
    from openeight._framework._runtime_models import model_is_configurable

    if isinstance(model, str) or model_is_configurable(model):
        issues.append(
            CompatibilityIssue(
                code="model_resolution_required",
                path=("model",),
                severity="deferred",
                message="Model resolution requires session startup",
            )
        )
    if governance.sources:
        issues.append(
            CompatibilityIssue(
                code="source_discovery_required",
                path=("sources",),
                severity="deferred",
                message="Source discovery requires session startup",
            )
        )
    if resources is not None:
        issues.append(
            CompatibilityIssue(
                code="resource_inspection_required",
                path=("resources",),
                severity="deferred",
                message="Application resources require session startup",
            )
        )
    if checkpointer is not None:
        kind = (type(checkpointer).__module__, type(checkpointer).__name__)
        allowed = {("langgraph.checkpoint.sqlite.aio", "AsyncSqliteSaver")}
        if checkpoints is not None:
            allowed.add(("langgraph.checkpoint.postgres.aio", "AsyncPostgresSaver"))
        if kind not in allowed:
            issues.append(
                CompatibilityIssue(
                    code="unsupported_checkpointer",
                    path=("checkpointer",),
                    severity="error",
                    message="Checkpointer kind is unsupported",
                )
            )
        else:
            issues.append(
                CompatibilityIssue(
                    code="checkpoint_inspection_required",
                    path=("checkpointer",),
                    severity="deferred",
                    message="Saver configuration and ownership require session startup",
                )
            )
    if middleware or state_schema is not None or context_schema is not None:
        issues.append(
            CompatibilityIssue(
                code="graph_inspection_required",
                path=("middleware",),
                severity="deferred",
                message="Effective native graph requires session startup inspection",
            )
        )
    return report(issues)
