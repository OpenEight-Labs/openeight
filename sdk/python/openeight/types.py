"""Immutable correlation and telemetry values for the machine SDK."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from openeight._json import snapshot_json
from openeight.errors import ConfigurationError

_INT64_MAX = (1 << 63) - 1
_EVENT_KINDS = frozenset({"span", "llm_call", "tool_call", "lifecycle"})
_OPERATION_KINDS = frozenset({"unspecified", "invoke", "stream"})
_OUTCOMES = frozenset({"unspecified", "success", "error", "interrupted"})
_VERDICTS = frozenset({"allow", "deny", "approval_required", "dry_run_only"})
_RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _configuration_error(reason: str) -> ConfigurationError:
    return ConfigurationError(reason)


def _validate_event_uuid7(value: Any) -> str:
    try:
        parsed = uuid.UUID(value) if isinstance(value, str) else None
    except (AttributeError, TypeError, ValueError):
        parsed = None
    if (
        parsed is None
        or parsed.version != 7
        or parsed.variant != uuid.RFC_4122
        or str(parsed) != value
    ):
        raise _configuration_error("invalid_event") from None
    return value


def _validate_event_uuid(value: Any) -> str:
    try:
        parsed = uuid.UUID(value)
        if parsed.variant != uuid.RFC_4122 or parsed.int == 0 or str(parsed) != value:
            raise ValueError
    except Exception:
        raise _configuration_error("invalid_event") from None
    return value


def _validate_event_name(value: Any, maximum: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > maximum
    ):
        raise _configuration_error("invalid_event") from None
    return value


def _optional_event_name(value: Any, maximum: int = 256) -> str | None:
    if value is None:
        return None
    return _validate_event_name(value, maximum)


def _normalize_event_timestamp(value: Any) -> str:
    parsed: datetime | None = None
    normalized: str | None = None
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and _RFC3339_PATTERN.fullmatch(value):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if (
            parsed is not None
            and parsed.tzinfo is not None
            and parsed.utcoffset() is not None
        ):
            normalized = (
                parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            )
    except Exception:
        pass
    if normalized is None:
        raise _configuration_error("invalid_event") from None
    return normalized


def _snapshot_event_json(value: Any, *, default: Any = None) -> bytes:
    if value is None:
        value = default
    return snapshot_json(value, "invalid_event")


def _safe_event_output(value: Any) -> Any:
    try:
        return json.loads(_snapshot_event_json(value))
    except ConfigurationError:
        return {"kind": "unavailable"}


def _validate_event_metric(value: Any) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _INT64_MAX
    ):
        raise _configuration_error("invalid_event_metrics") from None
    return value


@dataclass(frozen=True)
class DecisionResult:
    """Stored attempt verdict; approval replay also needs approved state."""

    decision_id: str
    verdict: str
    reason: str
    run_id: str
    replayed: bool

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"

    @property
    def needs_approval(self) -> bool:
        return self.verdict == "approval_required"


@dataclass(frozen=True, init=False, repr=False)
class Event:
    """Validated telemetry snapshot; later changes to caller input do not alter it."""

    name: str
    observation_kind: str
    operation_kind: str
    occurred_at: str
    outcome: str
    span_role: str | None
    parent_event_id: str | None
    decision_id: str | None
    provider: str | None
    model: str | None
    tool_id: str | None
    tool_version_id: str | None
    tool_name: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None
    latency_ms: int | None
    ttft_ms: int | None
    _input_bytes: bytes = field(repr=False)
    _output_bytes: bytes = field(repr=False)
    _error_bytes: bytes = field(repr=False)
    _attributes_bytes: bytes = field(repr=False)

    def __init__(
        self,
        name: str,
        observation_kind: str,
        operation_kind: str,
        occurred_at: datetime | str,
        outcome: str,
        parent_event_id: str | None = None,
        decision_id: str | None = None,
        input: Any = None,
        output: Any = None,
        error: Any = None,
        attributes: Any = None,
        provider: str | None = None,
        model: str | None = None,
        tool_id: str | None = None,
        tool_version_id: str | None = None,
        tool_name: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_creation_input_tokens: int | None = None,
        cache_read_input_tokens: int | None = None,
        latency_ms: int | None = None,
        ttft_ms: int | None = None,
        *,
        span_role: str | None = None,
    ) -> None:
        normalized_name = _validate_event_name(name)
        if observation_kind not in _EVENT_KINDS:
            raise _configuration_error("invalid_event")
        if operation_kind not in _OPERATION_KINDS:
            raise _configuration_error("invalid_event")
        if outcome not in _OUTCOMES:
            raise _configuration_error("invalid_event")
        if span_role is not None and (
            observation_kind != "span"
            or span_role not in {"graph", "node", "retriever", "internal"}
        ):
            raise _configuration_error("invalid_event")
        if outcome == "interrupted" and (
            observation_kind not in {"span", "lifecycle"}
            or error is not None
            or output is not None
        ):
            raise _configuration_error("invalid_event")
        if observation_kind == "lifecycle" and (
            operation_kind != "unspecified"
            or outcome not in {"interrupted", "unspecified"}
            or any(
                value is not None
                for value in (
                    input,
                    output,
                    error,
                    provider,
                    model,
                    input_tokens,
                    output_tokens,
                    cache_creation_input_tokens,
                    cache_read_input_tokens,
                    latency_ms,
                    ttft_ms,
                )
            )
        ):
            raise _configuration_error("invalid_event")
        normalized_time = _normalize_event_timestamp(occurred_at)
        normalized_parent = (
            _validate_event_uuid7(parent_event_id)
            if parent_event_id is not None
            else None
        )
        normalized_decision = (
            _validate_event_uuid7(decision_id) if decision_id is not None else None
        )
        normalized_tool_version = tool_version_id
        if normalized_tool_version is not None:
            _validate_event_uuid(normalized_tool_version)
        normalized_tool_id = (
            _validate_event_uuid(tool_id) if tool_id is not None else None
        )
        normalized_tool_name = _optional_event_name(tool_name)
        if normalized_tool_version is not None and (
            normalized_tool_id is None or normalized_tool_name is None
        ):
            raise _configuration_error("invalid_event") from None
        if normalized_tool_id is not None and normalized_tool_version is None:
            raise _configuration_error("invalid_event") from None

        metrics = {
            "input_tokens": _validate_event_metric(input_tokens),
            "output_tokens": _validate_event_metric(output_tokens),
            "cache_creation_input_tokens": _validate_event_metric(
                cache_creation_input_tokens
            ),
            "cache_read_input_tokens": _validate_event_metric(cache_read_input_tokens),
            "latency_ms": _validate_event_metric(latency_ms),
            "ttft_ms": _validate_event_metric(ttft_ms),
        }
        if (
            metrics["cache_creation_input_tokens"] is not None
            or metrics["cache_read_input_tokens"] is not None
        ) and metrics["input_tokens"] is None:
            raise _configuration_error("invalid_event_metrics")
        cache_total = (metrics["cache_creation_input_tokens"] or 0) + (
            metrics["cache_read_input_tokens"] or 0
        )
        if (
            metrics["input_tokens"] is not None
            and cache_total > metrics["input_tokens"]
        ):
            raise _configuration_error("invalid_event_metrics")
        if metrics["ttft_ms"] is not None and observation_kind != "llm_call":
            raise _configuration_error("invalid_event_metrics")
        if (
            metrics["ttft_ms"] is not None
            and metrics["latency_ms"] is not None
            and metrics["ttft_ms"] > metrics["latency_ms"]
        ):
            raise _configuration_error("invalid_event_metrics")
        if outcome == "success" and error is not None:
            raise _configuration_error("invalid_event")
        if outcome == "error" and error is None:
            raise _configuration_error("invalid_event")

        normalized_attributes = {} if attributes is None else attributes
        if type(normalized_attributes) is not dict:
            raise _configuration_error("invalid_event") from None

        values = {
            "span_role": span_role,
            "name": normalized_name,
            "observation_kind": observation_kind,
            "operation_kind": operation_kind,
            "occurred_at": normalized_time,
            "outcome": outcome,
            "parent_event_id": normalized_parent,
            "decision_id": normalized_decision,
            "provider": _optional_event_name(provider),
            "model": _optional_event_name(model),
            "tool_id": normalized_tool_id,
            "tool_version_id": normalized_tool_version,
            "tool_name": normalized_tool_name,
            **metrics,
            "_input_bytes": _snapshot_event_json(input),
            "_output_bytes": _snapshot_event_json(output),
            "_error_bytes": _snapshot_event_json(error),
            "_attributes_bytes": _snapshot_event_json(normalized_attributes),
        }
        for key, value in values.items():
            object.__setattr__(self, key, value)

    @staticmethod
    def _decode(value: bytes) -> Any:
        return json.loads(value)

    @property
    def input(self) -> Any:
        return self._decode(self._input_bytes)

    @property
    def output(self) -> Any:
        return self._decode(self._output_bytes)

    @property
    def error(self) -> Any:
        return self._decode(self._error_bytes)

    @property
    def attributes(self) -> Any:
        return self._decode(self._attributes_bytes)

    def _as_write(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "observation_kind": self.observation_kind,
            "operation_kind": self.operation_kind,
            "occurred_at": self.occurred_at,
            "outcome": self.outcome,
            "input_json": self._input_bytes.decode("utf-8"),
            "attributes_json": self._attributes_bytes.decode("utf-8"),
        }
        if self.outcome != "error" or self._output_bytes != b"null":
            result["output_json"] = self._output_bytes.decode("utf-8")
        if self.outcome == "error":
            result["error_json"] = self._error_bytes.decode("utf-8")
        if self.observation_kind == "lifecycle":
            result.pop("input_json", None)
            result.pop("output_json", None)
        if self.outcome == "interrupted":
            result.pop("output_json", None)
        for name in (
            "span_role",
            "parent_event_id",
            "decision_id",
            "provider",
            "model",
            "tool_id",
            "tool_version_id",
            "tool_name",
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "latency_ms",
            "ttft_ms",
        ):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        return result

    def __repr__(self) -> str:
        return (
            "Event("
            f"name={self.name!r}, observation_kind={self.observation_kind!r}, "
            f"operation_kind={self.operation_kind!r}, occurred_at={self.occurred_at!r}, "
            f"outcome={self.outcome!r}, decision_id={self.decision_id!r}, "
            f"latency_ms={self.latency_ms!r}, ttft_ms={self.ttft_ms!r})"
        )


def validate_decision_result(value: Any) -> DecisionResult:
    if not isinstance(value, dict):
        raise ValueError
    decision_id = value.get("decision_id")
    run_id = value.get("run_id")
    verdict = value.get("verdict")
    reason = value.get("reason")
    replayed = value.get("replayed")
    try:
        _validate_event_uuid7(decision_id)
        _validate_event_uuid7(run_id)
    except ConfigurationError:
        raise ValueError from None
    if (
        verdict not in _VERDICTS
        or not isinstance(reason, str)
        or type(replayed) is not bool
    ):
        raise ValueError
    return DecisionResult(decision_id, verdict, reason, run_id, replayed)
