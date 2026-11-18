"""Immutable durable execution values; persistence remains application-owned."""

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal, Protocol

from openeight.content import JSONValue
from openeight.errors import ConfigurationError


@dataclass(frozen=True)
class DurableObserveRecord:
    """Retained telemetry bytes awaiting acknowledgement, independent of execution replay."""

    id: str
    thread_id: str
    kind: Literal["run_active", "event", "run_complete"]
    payload_json: str
    created_at: str
    status: str = "pending"


@dataclass(frozen=True)
class DurableThread:
    deployment_id: str
    id: str
    case_id: str
    run_id: str
    run_started_at: str
    created_at: str
    updated_at: str
    status: str = "active"
    checkpoint_id: str | None = None
    run_ended_at: str | None = None
    revision: int = 0
    model_calls: int = 0
    read_calls: int = 0
    active_command_id: str | None = None
    observation_incomplete: int = 0


@dataclass(frozen=True)
class DurableCommand:
    deployment_id: str
    id: str
    thread_id: str
    kind: str
    request_json: str
    created_at: str
    updated_at: str
    status: str = "accepted"
    reason_code: str | None = None
    recover_command_id: str | None = None
    input_recorded: int = 0
    input_checkpoint_id: str | None = None
    output_checkpoint_id: str | None = None
    revision: int = 0


@dataclass(frozen=True)
class DurableAttempt:
    """Frozen Decision input and execution state retained across approval and recovery."""

    deployment_id: str
    decision_id: str
    thread_id: str
    tool_call_id: str
    tool_name: str
    tool_id: str
    tool_version_id: str
    policy_contract_set_digest: str
    source_time: str
    event_id: str
    native_call_json: str
    decision_input_json: str
    event_template_json: str
    created_at: str
    updated_at: str
    request_format: str = "decision-request-v3"
    terminal_event_json: str | None = None
    pause_event_json: str | None = None
    resume_event_json: str | None = None
    idempotency_key: str | None = None
    status: str = "prepared"
    result_json: str | None = None
    reason_code: str | None = None
    revision: int = 0

    def __post_init__(self):
        import json

        from openeight._json import decision_envelope

        if self.request_format != "decision-request-v3":
            raise ConfigurationError("durable_request_format_unsupported")
        try:
            envelope = json.loads(self.decision_input_json)
            if (
                set(envelope)
                != {"input", "policy_contract_set_digest", "selected_values"}
                or envelope["policy_contract_set_digest"]
                != self.policy_contract_set_digest
            ):
                raise ValueError
            decision_envelope(**envelope)
        except Exception:
            raise ConfigurationError("durable_request_format_unsupported") from None


@dataclass(frozen=True)
class DurableTransition:
    status: str | None = None
    checkpoint_id: str | None = None
    model_call_increment: int = 0
    read_call_increment: int = 0
    observation_incomplete: bool = False


@dataclass(frozen=True)
class AttemptTransition:
    status: str
    reason_code: str | None = None
    event: DurableObserveRecord | None = None


@dataclass(frozen=True)
class AttemptSettlement:
    """Terminal handler result and optional telemetry committed by the execution store."""

    status: Literal["succeeded", "failed", "blocked"]
    result_json: str
    reason_code: str | None = None
    event: DurableObserveRecord | None = None


@dataclass(frozen=True)
class CommandSettlement:
    """Command and thread outcome applied against the expected thread revision."""

    status: str
    expected_thread_revision: int
    thread_status: str
    output_checkpoint_id: str | None
    visible_turns: tuple[str, ...] = ()
    reason_code: str | None = None
    recovered_command_id: str | None = None
    recovered_command_revision: int | None = None
    recovered_status: str | None = None
    recovered_turns: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconciliationResult:
    """External effect status; unknown must not be treated as safe to repeat."""

    status: Literal["confirmed", "not_executed", "unknown"]
    result_json: str | None = None

    def __post_init__(self):
        if self.status not in {"confirmed", "not_executed", "unknown"} or (
            (self.status == "confirmed") != (self.result_json is not None)
        ):
            from openeight import ConfigurationError

            raise ConfigurationError("invalid_reconciliation")


TurnDisposition = Literal["terminal", "waiting_customer"]


@dataclass(frozen=True)
class DurableAgentResult:
    """Committed command outcome and pending approvals for the application to handle."""

    thread_id: str
    run_id: str
    status: str
    pending_decision_ids: tuple[str, ...] = ()
    output: tuple[str, ...] = ()
    has_structured_response: bool = False
    structured_response: JSONValue | None = None


class DurableExecutionStore(Protocol):
    """Application persistence boundary for exclusive ownership and atomic transitions."""

    def acquire(self, thread_id: str) -> AbstractContextManager[None]: ...
    def load(self, thread_id: str) -> DurableThread | None: ...
    def create(self, thread: DurableThread, active: DurableObserveRecord) -> None: ...
    def command(self, thread_id: str, command_id: str) -> DurableCommand: ...
    def prior_customer_inputs(
        self, thread_id: str, command_id: str
    ) -> tuple[tuple[str, str], ...]: ...
    def begin_command(
        self,
        thread_id: str,
        command_id: str,
        expected_revision: int,
        expected_thread_revision: int,
    ) -> DurableCommand: ...
    def record_command_input(
        self,
        thread_id: str,
        command_id: str,
        expected_revision: int,
        checkpoint_id: str | None,
    ) -> DurableCommand: ...
    def settle_command(
        self,
        thread_id: str,
        command_id: str,
        expected_revision: int,
        settlement: CommandSettlement,
    ) -> DurableCommand: ...
    def enqueue_callback_event(
        self, thread_id: str, record: DurableObserveRecord
    ) -> None: ...
    def get_attempt(
        self, thread_id: str, tool_call_id: str
    ) -> DurableAttempt | None: ...
    def attempts(self, thread_id: str) -> tuple[DurableAttempt, ...]: ...
    def prepare(self, attempt: DurableAttempt) -> DurableAttempt: ...
    def transition(
        self, thread_id: str, expected_revision: int, update: DurableTransition
    ) -> DurableThread: ...
    def transition_attempt(
        self,
        thread_id: str,
        decision_id: str,
        expected_revision: int,
        update: AttemptTransition,
    ) -> DurableAttempt: ...
    def settle_attempt(
        self,
        thread_id: str,
        decision_id: str,
        expected_revision: int,
        settlement: AttemptSettlement,
    ) -> DurableAttempt: ...
    def begin_completion(
        self,
        thread_id: str,
        expected_revision: int,
        ended_at: str,
        record: DurableObserveRecord,
    ) -> DurableThread: ...
    def pending(self, thread_id: str) -> tuple[DurableObserveRecord, ...]: ...
    def acknowledge(self, thread_id: str, record_id: str) -> None: ...


@dataclass(frozen=True, kw_only=True)
class DurableDeploymentEnrollment:
    deployment_id: str
    format_version: int
    created_at: str
    manifest_json: str = field(repr=False)
    manifest_sha256: str = field(repr=False)


class DurableDeploymentBinding(Protocol):
    """Bind a store to a measured application revision under deployment ownership."""

    deployment_id: str
    revision: str
    execution_store: DurableExecutionStore

    def acquire_deployment(self) -> AbstractContextManager[None]: ...
    def deployment(self) -> DurableDeploymentEnrollment | None: ...
    def is_empty(self) -> bool: ...
    def bind_deployment(self, value: DurableDeploymentEnrollment) -> None: ...


@dataclass(frozen=True)
class DurableApprovalConfig:
    """Application persistence and reconciliation callbacks with bounded execution budgets."""

    store: DurableExecutionStore
    reconcile: Callable[[DurableAttempt], Awaitable[ReconciliationResult]]
    classify_turn: Callable[[str], Awaitable[TurnDisposition]]
    max_model_calls: int = 16
    max_tool_attempts: int = 64
    deployment_binding: DurableDeploymentBinding | None = field(
        default=None, repr=False, kw_only=True
    )

    def __post_init__(self):
        from openeight import ConfigurationError

        if (
            not callable(self.reconcile)
            or not callable(self.classify_turn)
            or type(self.max_model_calls) is not int
            or not 1 <= self.max_model_calls <= 16
            or type(self.max_tool_attempts) is not int
            or not 1 <= self.max_tool_attempts <= 64
        ):
            raise ConfigurationError("invalid_integration_configuration")
        if self.deployment_binding is not None:
            from ._deployment import validate_binding

            validate_binding(self.deployment_binding, self.store)
