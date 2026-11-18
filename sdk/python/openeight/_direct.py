"""Validation shared by direct request preparation and Run recovery."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, ParamSpec, TypeVar

from openeight._json import snapshot_json
from openeight._semver import is_semver
from openeight.errors import (
    ConfigurationError,
    SDKError,
    _fresh_safe_error,
    _safe_error_descriptor,
)


def timestamp(value: datetime | str, *, decision: bool = False) -> str:
    try:
        if isinstance(value, str):
            value = str(value)
            if not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
                value,
            ):
                raise ValueError
            zone = re.search(r"([+-])(\d{2}):(\d{2})$", value)
            if zone and (int(zone[2]) > 23 or int(zone[3]) > 59):
                raise ValueError
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError
        value = value.astimezone(timezone.utc)
        if decision and value == datetime.min.replace(tzinfo=timezone.utc):
            raise ValueError
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    except Exception:
        raise ConfigurationError("invalid_source_time") from None


def identifier(
    value: str, *, versions: set[int] | None = None, reason: str = "invalid_run_id"
) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError
        value = str(value)
        parsed = uuid.UUID(value)
        if (
            str(parsed) != value
            or parsed.int == 0
            or parsed.variant != uuid.RFC_4122
            or versions is not None
            and parsed.version not in versions
        ):
            raise ValueError
        return value
    except Exception:
        raise ConfigurationError(reason) from None


def workflow(workflow_id: str | None, version: str | None) -> dict[str, str]:
    if workflow_id is None and version is None:
        return {}
    if (
        not isinstance(version, str)
        or not is_semver(version)
        or not 1 <= len(version.encode()) <= 128
    ):
        raise ConfigurationError("invalid_workflow")
    return {
        "workflow_id": identifier(
            workflow_id, versions={5, 7}, reason="invalid_workflow"
        ),
        "workflow_version": version,
    }


def strict_object(text: str) -> dict[str, Any]:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        if not isinstance(text, str) or len(text.encode()) > 1048576:
            raise ValueError
        value = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        snapshot_json(value, "invalid_json", object_root=True)
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ConfigurationError("invalid_json") from None


def same_json(a: Any, b: Any) -> bool:
    if type(a) in {int, float} and type(b) in {int, float}:
        return a == b
    if type(a) is not type(b):
        return False
    if type(a) is dict:
        return a.keys() == b.keys() and all(same_json(a[k], b[k]) for k in a)
    if type(a) is list:
        return len(a) == len(b) and all(same_json(x, y) for x, y in zip(a, b))
    return a == b


_P = ParamSpec("_P")
_R = TypeVar("_R")


def safe_direct_errors(function: Callable[_P, _R]) -> Callable[_P, _R]:
    """Drop request-bearing frames when exporting a direct SDK failure."""

    @wraps(function)
    def call(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        failure = None
        try:
            return function(*args, **kwargs)
        except SDKError as error:
            failure = _safe_error_descriptor(error)
        del args, kwargs
        raise _fresh_safe_error(failure) from None

    return call
