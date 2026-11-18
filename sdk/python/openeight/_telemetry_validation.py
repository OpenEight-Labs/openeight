"""Validate direct wire records without changing their retained representation."""

from __future__ import annotations

import json
import re
from typing import Any

from openeight._direct import identifier, strict_object, timestamp, workflow
from openeight._json import snapshot_json
from openeight.errors import ConfigurationError
from openeight.types import (
    _EVENT_KINDS,
    _OPERATION_KINDS,
    _OUTCOMES,
    _normalize_event_timestamp,
    _optional_event_name,
    _validate_event_metric,
    _validate_event_name,
    _validate_event_uuid,
)

_METRICS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "latency_ms",
    "ttft_ms",
)


_EVENT_FIELDS = (
    "id",
    "run_id",
    "name",
    "observation_kind",
    "operation_kind",
    "outcome",
    "occurred_at",
    "span_role",
    "parent_event_id",
    "decision_id",
    "provider",
    "model",
    "tool_id",
    "tool_version_id",
    "tool_name",
    "input_json",
    "output_json",
    "error_json",
    "attributes_json",
    *_METRICS,
)
_RUN_FIELDS = (
    "id",
    "status",
    "started_at",
    "ended_at",
    "attributes_json",
    "workflow_id",
    "workflow_version",
    "parent_run_id",
)


def _canonical_fields(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """Validate both ProtoJSON spellings without rewriting the caller's wire data."""
    result = dict(row)
    for field in fields:
        head, *tail = field.split("_")
        alias = head + "".join(part.title() for part in tail)
        if alias == field or alias not in row:
            continue
        if field in row:
            raise ConfigurationError("invalid_event")
        result[field] = result.pop(alias)
    return result


def _enum(value: Any, choices: frozenset[str] | set[str]) -> None:
    if not isinstance(value, str) or value not in choices:
        raise ConfigurationError("invalid_event")


def _payloads(row: dict[str, Any]) -> dict[str, Any]:
    values = {}
    total = 0
    for key in ("input_json", "output_json", "error_json", "attributes_json"):
        if key not in row:
            continue
        text = row[key]
        try:
            if not isinstance(text, str):
                raise ValueError
            value = json.loads(
                text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
            )
            snapshot_json(value, "invalid_event", object_root=key == "attributes_json")
            total += len(text.encode())
            values[key] = value
        except (TypeError, ValueError, RecursionError, UnicodeError):
            raise ConfigurationError("invalid_event") from None
    if total > 1048576:
        raise ConfigurationError("invalid_event")
    return values


def _identity(row: dict[str, Any], payloads: dict[str, Any]) -> None:
    kind = row["observation_kind"]
    tool_id, version_id, tool_name, decision_id = (
        row.get(key)
        for key in ("tool_id", "tool_version_id", "tool_name", "decision_id")
    )
    has_identity = any(
        value is not None for value in (tool_id, version_id, tool_name, decision_id)
    )
    if kind not in {"tool_call", "lifecycle"}:
        if has_identity:
            raise ConfigurationError("invalid_event")
        return
    if kind == "lifecycle" and not has_identity:
        return
    registered = tool_id is not None or version_id is not None
    if tool_name is None or kind == "lifecycle" and not registered:
        raise ConfigurationError("invalid_event")
    if registered:
        if tool_id is None or version_id is None:
            raise ConfigurationError("invalid_event")
        _validate_event_uuid(tool_id)
        _validate_event_uuid(version_id)
    if decision_id is not None:
        if not registered:
            raise ConfigurationError("invalid_event")
        if kind == "tool_call" and (
            row["outcome"] not in {"success", "error"}
            or type(payloads.get("input_json")) is not dict
        ):
            raise ConfigurationError("invalid_event")


def _metrics(row: dict[str, Any]) -> dict[str, int | None]:
    values = {}
    for key in _METRICS:
        value = row.get(key)
        if isinstance(value, str):
            if not re.fullmatch(r"(?:0|[1-9][0-9]{0,18})", value):
                raise ConfigurationError("invalid_event_metrics")
            value = int(value)
        values[key] = _validate_event_metric(value)
    inputs, creation, read = (
        values[key]
        for key in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    )
    if (
        (creation is not None or read is not None)
        and inputs is None
        or inputs is not None
        and (creation or 0) + (read or 0) > inputs
    ):
        raise ConfigurationError("invalid_event_metrics")
    ttft, latency = values["ttft_ms"], values["latency_ms"]
    if ttft is not None and (
        row["observation_kind"] != "llm_call" or latency is not None and ttft > latency
    ):
        raise ConfigurationError("invalid_event_metrics")
    return values


def validate_event(row: dict[str, Any], *, require_identity: bool = True) -> None:
    row = _canonical_fields(row, _EVENT_FIELDS)
    if require_identity:
        identifier(row.get("id"), versions={7})
        identifier(row.get("run_id"), versions={7})
    for key, choices in (
        ("observation_kind", _EVENT_KINDS),
        ("operation_kind", _OPERATION_KINDS),
        ("outcome", _OUTCOMES),
    ):
        _enum(row.get(key), choices)
    role = row.get("span_role")
    if role is not None:
        _enum(role, {"graph", "node", "retriever", "internal"})
        if row["observation_kind"] != "span":
            raise ConfigurationError("invalid_event")
    _validate_event_name(row.get("name"))
    _normalize_event_timestamp(row.get("occurred_at"))
    for key in ("provider", "model", "tool_name"):
        _optional_event_name(row.get(key))
    for key in ("parent_event_id", "decision_id"):
        if key in row:
            identifier(row[key], versions={5, 7})
    payloads = _payloads(row)
    metrics = _metrics(row)
    kind, outcome = row["observation_kind"], row["outcome"]
    # Encoded JSON null is a present payload, unlike an omitted wire field.
    if (
        outcome == "success"
        and "error_json" in payloads
        or outcome == "error"
        and ("error_json" not in payloads or "output_json" in payloads)
    ):
        raise ConfigurationError("invalid_event")
    if outcome == "interrupted" and (
        kind not in {"span", "lifecycle"}
        or "output_json" in payloads
        or "error_json" in payloads
    ):
        raise ConfigurationError("invalid_event")
    if kind == "lifecycle" and (
        row["operation_kind"] != "unspecified"
        or outcome not in {"interrupted", "unspecified"}
        or any(key in payloads for key in ("input_json", "output_json", "error_json"))
        or any(value is not None for value in metrics.values())
        or row.get("provider") is not None
        or row.get("model") is not None
    ):
        raise ConfigurationError("invalid_event")
    _identity(row, payloads)


def validate_run(row: dict[str, Any]) -> None:
    row = _canonical_fields(row, _RUN_FIELDS)
    identifier(row.get("id"), versions={7})
    if not isinstance(row.get("status"), str) or row["status"] not in {
        "active",
        "completed",
    }:
        raise ConfigurationError("invalid_run")
    start = timestamp(row.get("started_at"))
    end = timestamp(row["ended_at"]) if "ended_at" in row else None
    if (
        row["status"] == "active"
        and end is not None
        or row["status"] == "completed"
        and (end is None or end < start)
    ):
        raise ConfigurationError("invalid_run")
    if "parent_run_id" in row:
        identifier(row["parent_run_id"], versions={5, 7})
    workflow(row.get("workflow_id"), row.get("workflow_version"))
    strict_object(row.get("attributes_json", "{}"))
