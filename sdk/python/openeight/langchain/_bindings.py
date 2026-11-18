"""Validated construction-time bindings shared by framework adapters."""

import base64
import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType

from langchain_core.tools import StructuredTool, Tool

from openeight._json import snapshot_json
from openeight._managed_types import (  # noqa: F401
    CatalogReadiness,
    CatalogSnapshot,
    CatalogStatus,
)
from openeight.errors import ConfigurationError
from openeight.tools import ToolRegistration

from ._catalog import tool_schema

_EXECUTION_METHODS = ("invoke", "ainvoke", "run", "arun", "_run", "_arun")


def mapped_name(path, name):
    raw = json.dumps(
        ["openeight-deep-v1", list(path), name],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return "deep_" + base64.b32encode(
        hashlib.sha256(raw).digest()
    ).decode().lower().rstrip("=")


def executable_identity(tool):
    base = StructuredTool if isinstance(tool, StructuredTool) else Tool
    if not isinstance(tool, base) or any(
        getattr(type(tool), method) is not getattr(base, method)
        or method in tool.__dict__
        for method in _EXECUTION_METHODS
    ):
        raise ConfigurationError("unsupported_tool_definition")
    return (
        tool.func,
        tool.coroutine,
        *(getattr(type(tool), key) for key in _EXECUTION_METHODS),
    )


@dataclass(frozen=True)
class ToolBinding:
    tool: object
    child_path: tuple
    native_name: str
    facts: ToolRegistration
    schema_bytes: bytes
    executable: tuple
    remote: object = None

    def matches(self, candidate, name):
        try:
            identity = executable_identity(candidate)
            return (
                candidate is self.tool
                and name == self.native_name
                and candidate.name == name
                and all(a is b for a, b in zip(identity, self.executable))
                and snapshot_json(tool_schema(candidate), "invalid_event")
                == self.schema_bytes
            )
        except Exception:
            return False


class AdapterBindings:
    """Capture only graphs whose adapter has verified every final dispatch gate."""

    def __init__(
        self,
        tools_by_path,
        *,
        deep=False,
        remotes=None,
        name_mapper=None,
        descriptions=None,
        max_tools=200,
        max_bytes=1048576,
    ):
        remotes = remotes or {}
        descriptions = descriptions or {}
        entries, native, size = {}, {}, 0
        for path, tools in tools_by_path.items():
            for tool in tools:
                remote = remotes.get(id(tool))
                if (
                    (path, tool.name) in native
                    or remote is None
                    and tool.name.startswith(("deep_", "mcp_"))
                ):
                    raise ConfigurationError("unsupported_tool_definition")
                schema = tool_schema(tool)
                raw = snapshot_json(schema, "invalid_event")
                name = (
                    (name_mapper or mapped_name)(path, tool.name)
                    if deep and remote is None
                    else tool.name
                )
                facts = ToolRegistration(
                    name=name,
                    description=descriptions.get(
                        (path, tool.name), tool.description or ""
                    ),
                    input_schema=schema,
                )
                if name in entries and (
                    remote is None
                    or entries[name].remote is not remote
                    or entries[name].facts != facts
                ):
                    raise ConfigurationError("unsupported_tool_definition")
                binding = ToolBinding(
                    tool, path, tool.name, facts, raw, executable_identity(tool), remote
                )
                entries[name] = binding
                native[path, tool.name] = binding
                size += len(raw) + len(name.encode()) + len(facts.description.encode())
                if len(entries) > max_tools or size > max_bytes:
                    raise ConfigurationError("tool_discovery_limit_exceeded")
        if set(descriptions) - set(native):
            raise ConfigurationError("invalid_tool_surface")
        self.entries = MappingProxyType(entries)
        self.native = MappingProxyType(native)
        self.facts = tuple(entry.facts for entry in entries.values())
        self.completeness, self.diagnostic = "complete", None

    def resolve(self, path, name):
        return self.native.get((path, name))

    def validate(self):
        if any(
            not entry.matches(entry.tool, entry.native_name)
            for entry in self.native.values()
        ):
            raise ConfigurationError("tool_binding_changed")

    def snapshot(self):
        return tuple(
            MappingProxyType(
                {
                    "child_path": entry.child_path,
                    "native_name": entry.native_name,
                    "name": entry.facts.name,
                    "input_schema": entry.facts.input_schema,
                    "schema_digest": "sha256:"
                    + hashlib.sha256(entry.schema_bytes).hexdigest(),
                    "binding_id": entry.remote.source.config.binding_id
                    if entry.remote
                    else None,
                    "source_id": entry.remote.source.config.source_id
                    if entry.remote
                    else None,
                    "remote_name": entry.remote.remote_name if entry.remote else None,
                }
            )
            for entry in self.native.values()
        )
