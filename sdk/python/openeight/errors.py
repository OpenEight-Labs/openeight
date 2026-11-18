"""Fixed, non-disclosing public SDK errors."""

from __future__ import annotations

from typing import Any

from openeight._error_catalog import GOVERNANCE_REASONS
from openeight._protocol_constants import API_KEY_PREFIX

_CONFIGURATION_MESSAGES = {
    "structured_output_validation_failed": "Structured output validation failed",
    "structured_output_projection_failed": "Structured output could not be projected safely",
    "integration_mode_mismatch": "Managed agent entry mode does not match this operation",
    "integration_sync_reentry": "Blocking managed operation cannot run from an event loop",
    "integration_busy": "Managed agent already owns an execution",
    "integration_resource_conflict": "Managed resource ownership is conflicting",
    "integration_resource_cleanup_failed": "Managed resource cleanup failed",
    "integration_loop_changed": "Managed agent belongs to another event loop",
    "tool_binding_changed": "Captured tool binding has changed",
    "checkpoint_thread_unowned": "Checkpoint thread is not owned by this context",
    "checkpoint_thread_busy": "Checkpoint thread is already executing",
    "checkpoint_resume_invalid": "Checkpoint resume is not admissible",
    "checkpoint_scope_invalid": "Checkpoint authority is unavailable or incompatible",
    "checkpoint_persistence_failed": "Checkpoint persistence failed",
    "unsafe_framework_diagnostics": "Framework state diagnostics must be disabled",
    "tool_catalog_stale": "Tool catalog freshness has expired",
    "tool_source_unavailable": "Tool source is unavailable",
    "mcp_tool_selection_missing": "Selected MCP tool is unavailable",
    "durable_deployment_mismatch": "Durable deployment does not match the current runtime",
    "durable_deployment_busy": "Durable deployment is already executing",
    "durable_deployment_unbound": "Durable deployment must be initialized explicitly",
    "durable_dependencies_missing": (
        "Durable execution requires qualified SQLite dependencies; "
        "for Deep Agents install openeight[deepagents-durable]"
    ),
    "unsupported_framework_feature": "Framework execution feature is unsupported",
    "unsupported_framework_version": "Framework version is unsupported",
    "unsupported_graph_shape": "Agent graph shape is unsupported",
    "unsupported_tool_definition": "Tool definition is unsupported",
    "tool_discovery_limit_exceeded": "Tool discovery limit is exceeded",
    "instrumentation_process_changed": "Instrumentation cannot be used after fork",
    "unsupported_execution_method": "Execution method is unsupported",
    "governance_unconfigured": "Tool governance is not configured",
    "governance_not_ready": "Tool governance is not ready",
    "invalid_telemetry_content_configuration": "Telemetry content configuration is invalid",
    "invalid_delivery_configuration": "Delivery configuration is invalid",
    "framework_dependency_missing": "Required framework dependencies are missing",
    "missing_base_url": "base_url is required",
    "missing_api_key": "api_key is required",
    "invalid_api_key": f"api_key must use {API_KEY_PREFIX} prefix",
    "missing_agent_name": "agent_name is required",
    "invalid_agent_integration": "agent integration is invalid",
    "invalid_agent_name": "agent_name is invalid",
    "invalid_base_url": "base_url is invalid",
    "invalid_timeout": "timeout must be finite and greater than zero",
    "invalid_decision_id": "decision_id must be a UUIDv7",
    "invalid_run_id": "run_id must be a UUIDv7",
    "invalid_decision_replay": "decision_id and source_time must be provided together",
    "invalid_workflow": "workflow_id and workflow_version must be provided together",
    "invalid_source_time": "source_time must be timezone-aware RFC3339",
    "invalid_tool_id": "tool_id must be a UUID",
    "invalid_tool_version_id": "tool_version_id must be a UUID",
    "tool_registration_conflict": "Tool publication requires operator intervention",
    "tool_publication_unavailable": "Tool catalog publication is unavailable",
    "tool_publication_unsettled": "Previous tool publication is unresolved",
    "conflicting_tool_definitions": "Tool discovery contains conflicting schemas",
    "invalid_tool_registration": "tool registration is invalid",
    "invalid_tool_registrations": "registrations must contain 1 to 200 items",
    "invalid_tool_version_ids": "tool_version_ids must contain at most 200 unique UUIDs",
    "invalid_input": "input must be a strict JSON object",
    "invalid_run": "Run identity is invalid",
    "invalid_json": "JSON value is invalid",
    "invalid_telemetry_limit": "Telemetry request exceeds its limit",
    "client_busy": "Previous client operation is unresolved",
    "invalid_runs": "runs must be a list of JSON objects",
    "invalid_events": "events must be a list of JSON objects",
    "invalid_telemetry_scope": "telemetry contains a forbidden scope selector",
    "invalid_event_type": "event must be an Event",
    "invalid_event": "event is invalid",
    "invalid_event_metrics": "event metrics are invalid",
    "run_already_registered": "Observe Run is already registered",
    "run_not_registered": "Observe Run is not registered",
    "invalid_writer_limit": "Observe writer limits must be finite and greater than zero",
    "invalid_writer_threshold": "Observe flush thresholds must not exceed configured caps",
    "invalid_observe_sender": "Observe sender is invalid",
    "invalid_completed_sender": "Observe completed sender is invalid",
    "invalid_observe_encoder": "Observe encoder is invalid",
    "invalid_integration_configuration": "LangChain integration configuration is invalid",
    "integration_not_entered": "LangChain instrumentation is not entered",
    "integration_closed": "LangChain instrumentation is closed",
    "invalid_resume_correlation": "durable resume correlation is invalid",
    "durable_attempt_already_invoked": "Durable attempt was already invoked",
    "durable_attempt_conflict": "Durable attempt conflicts with its recorded request",
    "durable_attempt_missing": "Durable attempt is missing",
    "durable_checkpoint_conflict": "Durable checkpoint conflicts with its command",
    "durable_checkpoint_missing": "Durable checkpoint is missing",
    "durable_command_conflict": "Durable command conflicts with its admission",
    "durable_limit_exhausted": "Durable execution limit is exhausted",
    "durable_persistence_failed": "Durable evidence persistence failed",
    "durable_reconciliation_required": "Durable reconciliation is required",
    "durable_recovery_required": "Explicit durable recovery is required",
    "durable_thread_missing": "Durable conversation is missing",
    "invalid_output": "Conversation output is invalid",
    "invalid_tool_result": "Tool result is invalid",
    "invalid_turn_disposition": "Conversation disposition is invalid",
    "invalid_reconciliation": "Reconciliation result is invalid",
    "unsupported_checkpointer": "Durable checkpointer is unsupported",
    "execution_busy": "LangChain execution is busy",
    "client_closed": "SDK client is closed",
    "run_closed": "Governed Run is closed",
    "writer_closed": "Observe writer is closed",
}

_REST_ERROR_DEFINITIONS = {
    "invalid_agent_integration": (400, "bad_request"),
    "agent_integration_mismatch": (409, "conflict"),
    "TRIAL_EXPIRED": (403, "forbidden"),
    "AGENT_LIMIT_REACHED": (403, "forbidden"),
    "USER_LIMIT_REACHED": (403, "forbidden"),
    "DECISION_LIMIT_REACHED": (403, "forbidden"),
    "ACCESS_UNAVAILABLE": (503, "service_unavailable"),
    "AMBIGUOUS_WORKSPACE": (400, "bad_request"),
    "AUTH_NOT_CONFIGURED": (503, "service_unavailable"),
    "AUTH_VALIDATION_UNAVAILABLE": (503, "service_unavailable"),
    "BAD_GATEWAY": (502, "bad_gateway"),
    "BAD_REQUEST": (400, "bad_request"),
    "CONFLICT": (409, "conflict"),
    "TOOL_NOT_GOVERNED": (409, "conflict"),
    "EXECUTION_BLOCKED": (409, "conflict"),
    "AUTHORIZATION_STALE": (409, "conflict"),
    "policy_input_contract_stale": (409, "conflict"),
    "policy_evidence_too_large": (422, "unprocessable_entity"),
    "FORBIDDEN": (403, "forbidden"),
    "INTERNAL_ERROR": (500, "internal_error"),
    "NOT_FOUND": (404, "not_found"),
    "PAYLOAD_TOO_LARGE": (413, "payload_too_large"),
    "POLICY_INVALID": (422, "unprocessable_entity"),
    "RATE_LIMITED": (429, "rate_limited"),
    "SERVICE_UNAVAILABLE": (503, "service_unavailable"),
    "UNAUTHENTICATED": (401, "unauthorized"),
    "UNSUPPORTED_MEDIA_TYPE": (415, "unsupported_media_type"),
    "UNPROCESSABLE_ENTITY": (422, "unprocessable_entity"),
    "USER_NOT_ACTIVE": (403, "forbidden"),
    "WORKSPACE_FORBIDDEN": (403, "forbidden"),
}

_REST_CATEGORIES = frozenset(
    category for _, category in _REST_ERROR_DEFINITIONS.values()
)
_CONNECT_CODES = frozenset(
    {
        "canceled",
        "unknown",
        "invalid_argument",
        "deadline_exceeded",
        "not_found",
        "already_exists",
        "permission_denied",
        "resource_exhausted",
        "failed_precondition",
        "aborted",
        "out_of_range",
        "unimplemented",
        "internal",
        "unavailable",
        "data_loss",
        "unauthenticated",
    }
)


class SDKError(Exception):
    """Base class for every public SDK failure."""

    def __init__(self) -> None:
        self.message = "SDK operation failed"
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"{type(self).__name__}(message={self.message!r})"


class ConfigurationError(SDKError):
    def __init__(self, reason: str) -> None:
        if reason not in _CONFIGURATION_MESSAGES:
            reason = "invalid_configuration"
            message = "invalid configuration"
        else:
            message = _CONFIGURATION_MESSAGES[reason]
        self.reason = reason
        self.message = message
        Exception.__init__(self, self.message)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(reason={self.reason!r}, message={self.message!r})"
        )


class CleanupIncompleteError(SDKError):
    """Execution may have completed; reconcile outcomes before attempting it again."""

    def __init__(self, *, operation) -> None:
        self.code = "cleanup_incomplete"
        self._operation = operation
        self.message = "Managed agent cleanup is incomplete"
        Exception.__init__(self, self.message)

    @property
    def operation(self):
        return self._operation


class ResourceCleanupError(ConfigurationError):
    """A terminal application resource exit is never repeated."""

    def __init__(self, *, operation) -> None:
        super().__init__("integration_resource_cleanup_failed")
        self.code = self.reason
        self._operation = operation

    @property
    def operation(self):
        return self._operation


class APIError(SDKError):
    def __init__(
        self,
        status_code: int,
        category: str,
        code: str | None,
    ) -> None:
        if isinstance(status_code, bool) or not isinstance(status_code, int):
            status_code = 500
        if not isinstance(category, str) or (
            category not in _REST_CATEGORIES and category not in _CONNECT_CODES
        ):
            category = "internal_error"
            code = None
        elif code is not None and isinstance(code, str):
            rest_definition = _REST_ERROR_DEFINITIONS.get(code)
            connect_code = code if code in _CONNECT_CODES and code == category else None
            if (
                rest_definition != (status_code, category)
                and connect_code is None
                and GOVERNANCE_REASONS.get(code) != (status_code, category)
            ):
                code = None
        else:
            code = None
        self._retry_after = 0.0
        self.status_code = status_code
        self.category = category
        self.code = code
        self.message = "request failed"
        Exception.__init__(self, self.message)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(status_code={self.status_code!r}, "
            f"category={self.category!r}, code={self.code!r}, "
            f"message={self.message!r})"
        )


class ProtocolError(SDKError):
    def __init__(self, status_code: int | None = None) -> None:
        if isinstance(status_code, bool) or not isinstance(status_code, int):
            status_code = None
        self.status_code = status_code
        self.message = "invalid server response"
        Exception.__init__(self, self.message)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(status_code={self.status_code!r}, "
            f"message={self.message!r})"
        )


class TransportError(SDKError):
    def __init__(self) -> None:
        self.message = "transport request failed"
        Exception.__init__(self, self.message)


class BlockedError(SDKError):
    def __init__(self, result: Any, state: str | None = None) -> None:
        self.result = result
        self.state = state if state is not None else result.verdict
        self.message = "action blocked"
        Exception.__init__(self, self.message)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(state={self.state!r}, message={self.message!r})"


class ApprovalTimeout(SDKError):
    def __init__(self, result: Any) -> None:
        self.result = result
        self.state = "approval_wait_timed_out"
        self.message = "approval timed out"
        Exception.__init__(self, self.message)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(state={self.state!r}, message={self.message!r})"


class ObserveQueueFullError(SDKError):
    _MESSAGES = {
        "run_registry_full": "Observe Run registry is full",
        "write_queue_full": "Observe write queue is full",
        "observe_queue_full": "Observe queue is full",
    }

    def __init__(self, reason: str) -> None:
        if reason not in self._MESSAGES:
            reason = "observe_queue_full"
        self.reason = reason
        self.message = self._MESSAGES[reason]
        Exception.__init__(self, self.message)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(reason={self.reason!r}, message={self.message!r})"
        )


class ObserveDeliveryError(SDKError):
    _MESSAGES = {
        "flush_timeout": "Observe write flush timed out",
        "delivery_failed": "Observe write delivery failed",
        "record_too_large": "Observe record exceeds server limit",
        "suspend_flush_failed": "Governed Run suspension flush failed",
        "suspend_fresh_delivery_required": "Governed Run requires fresh delivery",
    }

    def __init__(self, reason: str) -> None:
        if reason not in self._MESSAGES:
            reason = "delivery_failed"
        self.reason = reason
        self.message = self._MESSAGES[reason]
        Exception.__init__(self, self.message)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(reason={self.reason!r}, message={self.message!r})"
        )


class ObserveRunCompletedError(SDKError):
    def __init__(self) -> None:
        self.reason = "run_completed"
        self.message = "Observe Run is completed"
        Exception.__init__(self, self.message)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(reason={self.reason!r}, message={self.message!r})"
        )


def _safe_error_descriptor(error: BaseException) -> tuple[Any, ...]:
    """Capture only fixed SDK-owned fields needed to create a fresh error."""
    if isinstance(error, (CleanupIncompleteError, ResourceCleanupError)):
        return (type(error), error.operation)
    if isinstance(error, APIError):
        return (
            APIError,
            error.status_code,
            error.category,
            error.code,
            error._retry_after,
        )
    if isinstance(error, ProtocolError):
        return (ProtocolError, error.status_code)
    if isinstance(error, TransportError):
        return (TransportError,)
    if isinstance(error, ConfigurationError):
        return (ConfigurationError, error.reason)
    if isinstance(error, BlockedError):
        return (BlockedError, error.result, error.state)
    if isinstance(error, ApprovalTimeout):
        return (ApprovalTimeout, error.result)
    if isinstance(error, ObserveRunCompletedError):
        return (ObserveRunCompletedError,)
    if isinstance(error, SDKError):
        reason = getattr(error, "reason", None)
        if isinstance(reason, str):
            return (type(error), reason)
        return (SDKError,)
    return (SDKError,)


def _fresh_safe_error(descriptor: tuple[Any, ...]) -> SDKError:
    error_type, *arguments = descriptor
    retry_after = (
        arguments.pop() if error_type is APIError and len(arguments) == 4 else 0.0
    )
    try:
        error = (
            error_type(operation=arguments[0])
            if error_type in {CleanupIncompleteError, ResourceCleanupError}
            else error_type(*arguments)
        )
        if isinstance(error, APIError):
            error._retry_after = retry_after
    except Exception:
        error = SDKError()
    return error if isinstance(error, SDKError) else SDKError()
