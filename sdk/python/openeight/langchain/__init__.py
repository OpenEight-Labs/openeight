"""Optional native LangChain observation and enforcement adapter."""

from openeight._managed_types import ManagedResourceFactory, ManagedResources

from ._builder import create_agent, observe
from .configuration import ApprovalConfig
from .durable import (
    AttemptSettlement,
    AttemptTransition,
    CommandSettlement,
    DurableAgentResult,
    DurableApprovalConfig,
    DurableAttempt,
    DurableCommand,
    DurableDeploymentBinding,
    DurableDeploymentEnrollment,
    DurableExecutionStore,
    DurableObserveRecord,
    DurableThread,
    DurableTransition,
    ReconciliationResult,
    TurnDisposition,
)

__all__ = [
    "ManagedResources",
    "ManagedResourceFactory",
    "create_agent",
    "observe",
    "ApprovalConfig",
    "DurableApprovalConfig",
    "DurableExecutionStore",
    "DurableDeploymentBinding",
    "DurableDeploymentEnrollment",
    "DurableAgentResult",
    "DurableAttempt",
    "DurableCommand",
    "DurableThread",
    "DurableObserveRecord",
    "DurableTransition",
    "AttemptTransition",
    "AttemptSettlement",
    "CommandSettlement",
    "ReconciliationResult",
    "TurnDisposition",
]
