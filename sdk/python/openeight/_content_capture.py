"""Local opt-in capture; application code runs without SDK locks."""

import inspect
import json
import math
from dataclasses import dataclass, field
from typing import Callable, Literal

from openeight.content import JSONValue, TelemetryContentContext
from openeight.errors import ConfigurationError

CONTENT_LIMIT = 16384
FAILURE_REASONS = (
    "unsupported_content",
    "payload_limit",
    "redaction_failed",
    "invalid_redaction_result",
)


@dataclass(frozen=True)
class _ContentPolicy:
    mode: Literal["none", "inputs", "outputs", "all"] = "none"
    redactor: (
        Callable[[TelemetryContentContext, JSONValue], JSONValue | None] | None
    ) = field(default=None, repr=False)

    def enabled(self, direction):
        return self.mode == "all" or self.mode == direction + "s"


def validate_content_policy(capture_content, redact_content):
    if (
        type(capture_content) is not str
        or capture_content not in ("none", "inputs", "outputs", "all")
        or redact_content is not None
        and not callable(redact_content)
    ):
        raise ConfigurationError("invalid_telemetry_content_configuration")
    return _ContentPolicy(capture_content, redact_content)


class _PayloadLimit(Exception):
    pass


def _snapshot(value):
    active = set()
    size = 0

    def charge(amount):
        nonlocal size
        size += amount
        if size > CONTENT_LIMIT:
            raise _PayloadLimit

    def copy(item, depth=0):
        if (
            item is None
            or type(item) is bool
            or type(item) is int
            and -(1 << 63) <= item < (1 << 63)
            or type(item) is float
            and math.isfinite(item)
            or type(item) is str
        ):
            charge(len(json.dumps(item, ensure_ascii=False, allow_nan=False).encode()))
            return item
        if type(item) not in (dict, list) or depth >= 32 or id(item) in active:
            raise ValueError
        active.add(id(item))
        try:
            charge(2 + max(0, len(item) - 1))
            if type(item) is list:
                return [copy(child, depth + 1) for child in item]
            result = {}
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError
                copy(key, depth + 1)
                charge(1)
                result[key] = copy(child, depth + 1)
            return result
        finally:
            active.remove(id(item))

    try:
        return copy(value), None
    except _PayloadLimit:
        return None, "payload_limit"


def capture_optional_content(policy, context, project):
    if not policy.enabled(context.direction):
        return None, None
    try:
        value, reason = _snapshot(project())
    except Exception:
        return None, "unsupported_content"
    if reason or policy.redactor is None:
        return value, reason
    try:
        value = policy.redactor(context, value)
    except Exception:
        return None, "redaction_failed"
    if value is None:
        return None, None
    try:
        if inspect.isawaitable(value):
            if inspect.iscoroutine(value):
                value.close()
            return None, "invalid_redaction_result"
        return _snapshot(value)
    except Exception:
        return None, "invalid_redaction_result"
