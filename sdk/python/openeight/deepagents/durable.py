"""Public durable contracts for the exactly qualified Deep Agents integration."""

import re
import unicodedata
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from openeight.errors import ConfigurationError
from openeight.langchain.durable import (
    AttemptSettlement,
    DurableAgentResult,
    DurableApprovalConfig,
    DurableAttempt,
    DurableExecutionStore,
    ReconciliationResult,
    TurnDisposition,
)
from openeight.langchain.durable import (
    AttemptTransition as AttemptTransition,
)
from openeight.langchain.durable import (
    CommandSettlement as CommandSettlement,
)
from openeight.langchain.durable import (
    DurableCommand as DurableCommand,
)
from openeight.langchain.durable import (
    DurableObserveRecord as DurableObserveRecord,
)
from openeight.langchain.durable import (
    DurableThread as DurableThread,
)
from openeight.langchain.durable import (
    DurableTransition as DurableTransition,
)


def _uuid(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ConfigurationError("invalid_integration_configuration") from None


def _hash(value):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ConfigurationError("invalid_integration_configuration")


@dataclass(frozen=True, kw_only=True)
class LocalDurability:
    """Opt in to SDK-owned local journals bound to an explicit application revision."""

    directory: str | Path
    revision: str
    reconcile: Callable[[DurableAttempt], Awaitable[ReconciliationResult]] | None = None
    classify_turn: Callable[[str], Awaitable[TurnDisposition]] | None = None
    max_model_calls: int = 16
    max_tool_attempts: int = 64

    def __post_init__(self):
        if (
            not isinstance(self.directory, (str, Path))
            or not str(self.directory)
            or type(self.revision) is not str
            or not self.revision.strip()
            or any(unicodedata.category(c) in {"Cc", "Cs"} for c in self.revision)
            or len(self.revision.encode()) > 256
            or self.reconcile is not None
            and not callable(self.reconcile)
            or self.classify_turn is not None
            and not callable(self.classify_turn)
            or type(self.max_model_calls) is not int
            or not 1 <= self.max_model_calls <= 16
            or type(self.max_tool_attempts) is not int
            or not 1 <= self.max_tool_attempts <= 64
        ):
            raise ConfigurationError("invalid_integration_configuration")


@dataclass(frozen=True)
class DurableThreadStatus:
    """Current local journal state, without command output or authorization grants."""

    thread_id: str
    run_id: str
    status: Literal[
        "active", "waiting", "waiting_customer", "completed", "reconciliation_required"
    ]
    pending_decision_ids: tuple[str, ...]


@dataclass(frozen=True)
class DeepAgentsDeploymentConfig:
    """Application-supplied build and configuration identities for deployment admission."""

    deployment_id: str
    application_build_sha256: str
    configuration_sha256: str

    def __post_init__(self):
        _uuid(self.deployment_id)
        _hash(self.application_build_sha256)
        _hash(self.configuration_sha256)


@dataclass(frozen=True)
class DurableDeployment:
    deployment_id: str
    format_version: int
    identity_json: str = field(repr=False)
    credential_binding_sha256: str = field(repr=False)
    manifest_json: str | None = field(repr=False)


@dataclass(frozen=True)
class DurableContinuation:
    """Retained native task identity linking a child continuation to its durable attempt."""

    deployment_id: str
    thread_id: str
    decision_id: str
    child_path_json: str
    checkpoint_namespace: str
    native_tool_call_id: str
    attempt_key: str
    kind: Literal["business_write", "state_write", "delegation"]
    parent_attempt_key: str | None = None


@dataclass(frozen=True)
class GeneratedStateReceipt:
    """Checkpoint-linked state update used to prove generated-effect recovery."""

    deployment_id: str
    thread_id: str
    decision_id: str
    format_version: int
    input_checkpoint_id: str
    update_json: str


class DeepAgentsExecutionStore(DurableExecutionStore, Protocol):
    """Persist continuations and settle generated results with their state receipts."""

    def acquire_deployment(self) -> AbstractContextManager[None]: ...
    def deployment(self) -> DurableDeployment: ...
    def bind_deployment(self, deployment_id: str, manifest_json: str) -> None: ...
    def prepare_continuation(
        self, attempt: DurableAttempt, continuation: DurableContinuation
    ) -> DurableAttempt: ...
    def continuation(
        self, thread_id: str, attempt_key: str
    ) -> DurableContinuation | None: ...
    def generated_state_receipt(
        self, thread_id: str, attempt_key: str
    ) -> GeneratedStateReceipt | None: ...
    def settle_generated_attempt(
        self,
        thread_id: str,
        attempt_key: str,
        expected_revision: int,
        settlement: AttemptSettlement,
        receipt: GeneratedStateReceipt,
    ) -> DurableAttempt: ...


def validate_store(store):
    required = {
        name
        for protocol in (DurableExecutionStore, DeepAgentsExecutionStore)
        for name, value in vars(protocol).items()
        if not name.startswith("_") and callable(value)
    }
    try:
        if any(not callable(getattr(store, name, None)) for name in required):
            raise ValueError
    except Exception:
        raise ConfigurationError("invalid_integration_configuration") from None


__all__ = [
    "LocalDurability",
    "DurableThreadStatus",
    "DeepAgentsDeploymentConfig",
    "DurableDeployment",
    "GeneratedStateReceipt",
    "DurableContinuation",
    "DeepAgentsExecutionStore",
    "DurableApprovalConfig",
    "DurableAgentResult",
    "DurableExecutionStore",
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
