"""Explicit construction surfaces and operator-facing catalog facts."""

import re
from dataclasses import dataclass

from langchain.agents.middleware import AgentMiddleware

from openeight import ConfigurationError

FS_NAMES = frozenset(
    {"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute"}
)


@dataclass(frozen=True)
class ToolSurface:
    """Exact executable native tool names for one root or child path."""

    child_path: tuple[str, ...]
    native_names: tuple[str, ...]


@dataclass(frozen=True)
class CatalogDescription:
    """Operator description; does not replace model instructions or grant authority."""

    child_path: tuple[str, ...]
    native_name: str
    description: str


def invalid():
    raise ConfigurationError("invalid_tool_surface")


def path_valid(path):
    return (
        type(path) is tuple
        and len(path) <= 16
        and all(type(p) is str and p for p in path)
    )


def surface_map(items):
    if items is None:
        return None
    result = {}
    for item in tuple(items):
        if (
            not isinstance(item, ToolSurface)
            or not path_valid(item.child_path)
            or item.child_path in result
            or type(item.native_names) is not tuple
            or any(type(n) is not str or not n for n in item.native_names)
            or len(set(item.native_names)) != len(item.native_names)
        ):
            invalid()
        result[item.child_path] = frozenset(item.native_names)
    return result


def descriptions_map(items):
    result = {}
    for item in tuple(items):
        if (
            not isinstance(item, CatalogDescription)
            or not path_valid(item.child_path)
            or type(item.native_name) is not str
            or not item.native_name
            or type(item.description) is not str
            or not item.description
            or item.description.strip() != item.description
            or len(item.description) > 240
            or len(item.description.splitlines()) != 1
            or (item.child_path, item.native_name) in result
        ):
            invalid()
        result[item.child_path, item.native_name] = item.description
    return result


def validate_qualified_path(path):
    if (path and path[0] == "coordinator") or any(
        re.fullmatch(r"[a-z][a-z0-9_-]*", part) is None for part in path
    ):
        invalid()


def qualified_name(path, name):
    validate_qualified_path(path)
    parts = (*path, name) if path else ("coordinator", name)
    if (path and path[0] == "coordinator") or any(
        re.fullmatch(r"[a-z][a-z0-9_-]*", part) is None for part in parts
    ):
        invalid()
    value = ".".join(parts)
    if len(value.encode()) > 256:
        invalid()
    return value


def select_tools(tools, middleware, names):
    """Select application tools; reject partial custom middleware surfaces."""
    selected = [
        t for t in tools if getattr(t, "name", getattr(t, "__name__", None)) in names
    ]
    for item in middleware:
        if not isinstance(item, AgentMiddleware):
            invalid()
        supplied = {t.name for t in item.tools}
        if supplied and not supplied <= names:
            invalid()
    return selected
