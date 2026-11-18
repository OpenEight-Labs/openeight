"""Optional managed Deep Agents integration."""

from openeight._managed_types import ManagedResourceFactory, ManagedResources

from ._builder import create_deep_agent, observe

__all__ = [
    "ManagedResources",
    "ManagedResourceFactory",
    "create_deep_agent",
    "observe",
    "CatalogDescription",
    "ToolSurface",
]

from ._surface import CatalogDescription, ToolSurface
from .durable import (
    AttemptSettlement,
    AttemptTransition,
    CommandSettlement,
    DeepAgentsDeploymentConfig,
    DeepAgentsExecutionStore,
    DurableAgentResult,
    DurableApprovalConfig,
    DurableAttempt,
    DurableCommand,
    DurableContinuation,
    DurableDeployment,
    DurableExecutionStore,
    DurableObserveRecord,
    DurableThread,
    DurableThreadStatus,
    DurableTransition,
    GeneratedStateReceipt,
    LocalDurability,
    ReconciliationResult,
    TurnDisposition,
)

__all__ += [
    "DurableExecutionStore",
    "DurableCommand",
    "DurableThread",
    "DurableThreadStatus",
    "DurableObserveRecord",
    "DurableTransition",
    "AttemptTransition",
    "CommandSettlement",
    "AttemptSettlement",
    "LocalDurability",
    "DeepAgentsDeploymentConfig",
    "DeepAgentsExecutionStore",
    "DurableDeployment",
    "DurableContinuation",
    "GeneratedStateReceipt",
    "DurableApprovalConfig",
    "DurableAgentResult",
    "DurableAttempt",
    "ReconciliationResult",
    "TurnDisposition",
]
