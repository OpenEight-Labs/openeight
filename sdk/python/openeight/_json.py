"""Strict immutable runtime JSON snapshots and mutable catalog projections."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

from openeight.errors import ConfigurationError

JSONInput = None | bool | int | float | str | list["JSONInput"] | dict[str, "JSONInput"]


def _copy_runtime_json(value: Any, active: set[int], depth: int = 0) -> JSONInput:
    if value is None or type(value) is bool:
        return value
    if type(value) is int and -(1 << 63) <= value < (1 << 63):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is str:
        if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > 65536:
            raise ValueError
        return value
    if type(value) not in {dict, list} or depth >= 32 or id(value) in active:
        raise ValueError
    active.add(id(value))
    try:
        if type(value) is list:
            return [_copy_runtime_json(item, active, depth + 1) for item in value]
        result = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError
            _copy_runtime_json(key, active)
            result[key] = _copy_runtime_json(item, active, depth + 1)
        return result
    finally:
        active.remove(id(value))


def copy_json_value(value: Any) -> JSONInput:
    """Copy one runtime JSON value, rejecting cycles and unsafe object types."""
    return _copy_runtime_json(value, set())


def snapshot_json(value: Any, reason: str, *, object_root: bool = False) -> bytes:
    """Copy strict runtime JSON and serialize it; fingerprinting is server-owned."""
    snapshot = None
    try:
        if object_root and type(value) is not dict:
            raise ValueError
        copied = copy_json_value(value)
        encoded = json.dumps(
            copied,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) <= 1048576:
            snapshot = encoded
    except Exception:
        pass
    if snapshot is None:
        raise ConfigurationError(reason) from None
    return snapshot


def thaw_json(value: Any) -> Any:
    """Convert frozen catalog mappings and tuples to JSON dictionaries and lists."""
    if isinstance(value, Mapping):
        return {k: thaw_json(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(v) for v in value]
    return value


def decision_envelope(
    input: Any, policy_contract_set_digest: str, selected_values: Any
) -> bytes:
    """Freeze the exact v3 input envelope without applying policy aliases."""
    import re

    if (
        type(input) is not dict
        or input
        or not isinstance(policy_contract_set_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", policy_contract_set_digest)
    ):
        raise ConfigurationError("invalid_input")
    if type(selected_values) not in {list, tuple}:
        raise ConfigurationError("invalid_input")
    values = []
    seen = set()
    for item in selected_values:
        if type(item) is not dict or set(item) != {"path", "value"}:
            raise ConfigurationError("invalid_input")
        path = item["path"]
        if (
            type(path) is not str
            or not path.strip()
            or len(path.encode()) > 256
            or "*" in path
            or any(not part for part in path.split("."))
            or path in seen
        ):
            raise ConfigurationError("invalid_input")
        seen.add(path)
        values.append(item)
    encoded = snapshot_json(
        {
            "input": {},
            "policy_contract_set_digest": policy_contract_set_digest,
            "selected_values": sorted(values, key=lambda item: item["path"]),
        },
        "invalid_input",
        object_root=True,
    )
    if len(encoded) > 1044480:
        raise ConfigurationError("invalid_input")
    return encoded
