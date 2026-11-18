"""Immutable local lifecycle and execution diagnostics."""

from dataclasses import dataclass
from typing import Literal

LifecycleState = Literal[
    "idle", "opening", "open", "executing-transient", "settling", "closed"
]
OperationPhase = Literal["startup", "execution", "settlement", "cleanup"]


@dataclass(frozen=True, kw_only=True)
class LifecycleStatus:
    """Local execution and cleanup state; resource closure does not prove effect settlement."""

    state: LifecycleState
    cleanup_complete: bool
    effects_settled: bool
    cleanup_terminal: bool
    error_code: str | None
    active_executions: int


@dataclass(frozen=True, kw_only=True)
class ExecutionOutcome:
    """Local outcome for one input, retaining its Run identity when available."""

    item_index: int
    run_id: str | None
    state: Literal["not_started", "completed", "failed", "cancelled", "unsettled"]


@dataclass(frozen=True, kw_only=True)
class OperationStatus:
    """Execution outcomes attached to a startup, execution or cleanup operation."""

    phase: OperationPhase
    run_ids: tuple[str, ...]
    outcomes: tuple[ExecutionOutcome, ...]
