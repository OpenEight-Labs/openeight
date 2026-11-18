"""Canonical configuration measurement without object or repr fallbacks."""

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from openeight.errors import ConfigurationError


def tagged(value):
    if value is None:
        return ["null"]
    if type(value) is bool:
        return ["bool", value]
    if type(value) is int:
        return ["int", str(value)]
    if type(value) is str:
        value.encode("utf-8")
        return ["str", value]
    if type(value) is float and math.isfinite(value):
        return ["float", value.hex()]
    if type(value) in (tuple, list):
        return ["list", [tagged(item) for item in value]]
    if isinstance(value, Mapping) and all(type(key) is str for key in value):
        return [
            "map",
            [
                [key, tagged(value[key])]
                for key in sorted(value, key=lambda key: key.encode("utf-8"))
            ],
        ]
    raise ValueError


def payload_hash(value):
    failed = False
    result = None
    try:
        raw = json.dumps(
            tagged(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(raw) > 1024 * 1024:
            raise ValueError
        result = hashlib.sha256(raw).hexdigest()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        failed = True
    if failed:
        raise ConfigurationError("unsupported_graph_shape")
    return result


def freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if type(value) in (list, tuple):
        return tuple(freeze(item) for item in value)
    return value


def plain(value):
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if type(value) in (list, tuple):
        return [plain(item) for item in value]
    return value


def qualified(value):
    cls = value if isinstance(value, type) else type(value)
    return cls.__module__ + "." + cls.__qualname__


@dataclass(frozen=True)
class MeasuredConfiguration:
    descriptor: Mapping
    sha256: str
