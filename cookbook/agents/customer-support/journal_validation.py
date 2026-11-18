"""Validation of frozen execution payloads before transactional settlement."""

import json
from uuid import UUID
from local import LocalError, canonical


def validate_uuid(value):
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise LocalError("invalid_input") from None


def parse_canonical_json(value, maximum=65536):
    try:
        parsed = json.loads(value)
        if (
            type(value) is not str
            or len(value.encode()) > maximum
            or canonical(parsed) != value
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise LocalError("invalid_input") from None
    return parsed


def validate_telemetry_json(value):
    """Validate optional telemetry independently of the native recovery result."""
    import math

    def validate(item, depth=0):
        if item is None or type(item) is bool:
            return
        if type(item) is int and -(1 << 63) <= item < (1 << 63):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is str:
            item.encode("utf-8")
            return
        if depth >= 32:
            raise ValueError
        if type(item) is list:
            for child in item:
                validate(child, depth + 1)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for key, child in item.items():
                validate(key, depth + 1)
                validate(child, depth + 1)
            return
        raise ValueError

    try:
        parsed = json.loads(value)
        validate(parsed)
        encoded = json.dumps(
            parsed,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        # Event JSON uses UTF-8, while the durable transport's outer JSON escapes it.
        if type(value) is not str or encoded != value or len(encoded.encode()) > 16384:
            raise ValueError
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise LocalError("invalid_input") from None
    return parsed


def validate_attempt_settlement(record, settlement, thread_id):
    event = settlement.event
    raw = event.payload_json if event else None
    native_result = parse_canonical_json(settlement.result_json)
    if (
        type(native_result) is not dict
        or set(native_result) != {"content", "status"}
        or type(native_result["content"]) is not str
        or native_result["status"]
        != ("success" if settlement.status == "succeeded" else "error")
    ):
        raise LocalError("invalid_settlement")
    if settlement.status == "blocked":
        if event is not None or record.status == "executing":
            raise LocalError("invalid_settlement")
    else:
        if (
            event is None
            or record.status != "executing"
            or event.id != record.event_id
            or event.thread_id != thread_id
            or event.kind != "event"
        ):
            raise LocalError("invalid_settlement")
        payload = parse_canonical_json(raw)
        template = parse_canonical_json(record.event_template_json)
        expected = {
            **template,
            "outcome": "success"
            if settlement.status == "succeeded"
            else "error",
        }
        expected.pop("output_json", None)
        if settlement.status == "succeeded" and "output_json" in payload:
            try:
                validate_telemetry_json(
                    payload["output_json"]
                )
            except LocalError:
                raise LocalError("invalid_settlement") from None
            expected["output_json"] = payload["output_json"]
        if settlement.status == "failed":
            expected["error_json"] = canonical({"kind": "tool_error"})
            expected.pop("output_json", None)
        from datetime import datetime

        try:
            datetime.fromisoformat(
                payload["occurred_at"].replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError):
            raise LocalError("invalid_settlement") from None
        expected["occurred_at"] = payload["occurred_at"]
        attributes = json.loads(payload.get("attributes_json", "{}"))
        if attributes.get("timing_source") == "receipt_recovery":
            expected["attributes_json"] = canonical(
                {
                    **json.loads(template.get("attributes_json", "{}")),
                    "timing_source": "receipt_recovery",
                }
            )
        elif "latency_ms" in payload:
            if (
                type(payload["latency_ms"]) is not int
                or payload["latency_ms"] < 0
            ):
                raise LocalError("invalid_settlement")
            expected["latency_ms"] = payload["latency_ms"]
        # Old frozen settlements without timing remain replayable.
        if payload != expected:
            raise LocalError("invalid_settlement")
