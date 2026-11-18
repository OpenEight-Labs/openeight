"""Strict generated-state receipt encoding, distinct from business results."""

import json
from datetime import datetime, timezone
from pathlib import PurePosixPath

from openeight.errors import ConfigurationError

MAX_RECEIPT_BYTES = 1024 * 1024


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _text(value):
    if type(value) is not str:
        raise ValueError
    value.encode("utf-8")


def _timestamp(value):
    _text(value)
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.utcoffset() != timezone.utc.utcoffset(stamp):
        raise ValueError


def encode_state_update(value):
    try:
        if type(value) is not dict or set(value) - {"files", "todos"}:
            raise ValueError
        if "files" in value:
            files = value["files"]
            if type(files) is not dict:
                raise ValueError
            for path, data in files.items():
                _text(path)
                if (
                    not path.startswith("/")
                    or path.startswith("//")
                    or path != str(PurePosixPath(path))
                    or ".." in PurePosixPath(path).parts
                ):
                    raise ValueError
                if data is None:
                    continue
                if type(data) is not dict or set(data) != {
                    "content",
                    "encoding",
                    "created_at",
                    "modified_at",
                }:
                    raise ValueError
                _text(data["content"])
                if data["encoding"] != "utf-8":
                    raise ValueError
                _timestamp(data["created_at"])
                _timestamp(data["modified_at"])
        if "todos" in value:
            if type(value["todos"]) is not list:
                raise ValueError
            for todo in value["todos"]:
                if type(todo) is not dict or set(todo) != {"content", "status"}:
                    raise ValueError
                _text(todo["content"])
                if todo["status"] not in {"pending", "in_progress", "completed"}:
                    raise ValueError
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(raw.encode()) > MAX_RECEIPT_BYTES:
            raise ValueError
        return raw
    except (ValueError, TypeError, KeyError, OverflowError, UnicodeError):
        raise ConfigurationError("durable_reconciliation_required") from None


def decode_state_update(raw):
    try:
        if type(raw) is not str or len(raw.encode()) > MAX_RECEIPT_BYTES:
            raise ValueError
        value = json.loads(raw, object_pairs_hook=_object)
        if encode_state_update(value) != raw:
            raise ValueError
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ConfigurationError("durable_reconciliation_required") from None
