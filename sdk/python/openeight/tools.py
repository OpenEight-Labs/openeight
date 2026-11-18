"""Immutable Python projections of the governed Tool catalog."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from openeight._json import JSONInput, copy_json_value, snapshot_json, thaw_json
from openeight.errors import ConfigurationError

_CAPABILITIES = {"", "read", "write"}
_STATUSES = {"registered", "replayed", "invalid", "conflict", "unavailable"}
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def _invalid_registration(
    reason: str = "invalid_tool_registration",
) -> ConfigurationError:
    return ConfigurationError(reason)


def _utf8_length(value: str) -> int:
    length: int | None = None
    try:
        length = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        pass
    if length is None:
        raise _invalid_registration() from None
    return length


def _freeze_catalog_json(value: Any, active: set[int] | None = None) -> Any:
    active = set() if active is None else active
    if value is None or type(value) is bool:
        return value
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise _invalid_registration() from None
        return value
    if type(value) is int:
        # Stored schema facts use JSON-safe integers; runtime tool inputs use int64.
        if not -(1 << 53) + 1 <= value <= (1 << 53) - 1:
            raise _invalid_registration()
        return value
    if type(value) is float:
        if value != value or value in {float("inf"), float("-inf")}:
            raise _invalid_registration()
        return value
    if type(value) not in {dict, list, tuple}:
        raise _invalid_registration()
    marker = id(value)
    if marker in active:
        raise _invalid_registration()
    active.add(marker)
    try:
        if isinstance(value, (list, tuple)):
            return tuple(_freeze_catalog_json(item, active) for item in value)
        if not all(type(key) is str for key in value):
            raise _invalid_registration()
        return MappingProxyType(
            {key: _freeze_catalog_json(item, active) for key, item in value.items()}
        )
    finally:
        active.remove(marker)


def _validate_catalog_timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise _invalid_registration()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.isoformat().replace("+00:00", "Z")
    except Exception:
        raise _invalid_registration() from None


def _validate_catalog_uuid(
    value: Any, reason: str = "invalid_tool_registration"
) -> str:
    import uuid

    try:
        parsed = uuid.UUID(value)
        if parsed.variant != uuid.RFC_4122 or str(parsed) != value:
            raise ValueError
    except Exception:
        raise _invalid_registration(reason) from None
    return value


def _optional_uuid(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return _validate_catalog_uuid(value)


@dataclass(frozen=True)
class FieldSelector:
    path: str
    alias: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.path, str)
            or not self.path.strip()
            or _utf8_length(self.path) > 256
            or "*" in self.path
            or self.path.startswith(".")
            or self.path.endswith(".")
            or ".." in self.path
        ):
            raise _invalid_registration()
        if self.alias is not None and (
            not isinstance(self.alias, str)
            or not self.alias.strip()
            or _utf8_length(self.alias) > 256
            or "." in self.alias
        ):
            raise _invalid_registration()


@dataclass(frozen=True, init=False)
class ToolRegistration:
    """Immutable schema facts with optional paired preconditions for a schema successor."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    expected_tool_id: str | None
    expected_current_version_id: str | None

    def __init__(
        self,
        *,
        name: str,
        description: str,
        input_schema: Mapping[str, Any],
        expected_tool_id: str | None = None,
        expected_current_version_id: str | None = None,
    ) -> None:
        if (expected_tool_id is None) != (expected_current_version_id is None):
            raise _invalid_registration()
        for value in (expected_tool_id, expected_current_version_id):
            if value is not None:
                _validate_catalog_uuid(value)
        object.__setattr__(self, "expected_tool_id", expected_tool_id)
        object.__setattr__(
            self, "expected_current_version_id", expected_current_version_id
        )
        valid = False
        frozen_schema = None
        try:
            if (
                type(name) is not str
                or not name.strip()
                or not 1 <= _utf8_length(name) <= 256
                or type(description) is not str
                or _utf8_length(description) > 4096
                or not isinstance(input_schema, Mapping)
            ):
                raise ValueError
            frozen_schema = _freeze_catalog_json(thaw_json(dict(input_schema)))
            _validate_schema(frozen_schema)
            raw = json.dumps(
                thaw_json(frozen_schema), ensure_ascii=False, allow_nan=False
            ).encode("utf-8")
            if len(raw) > 256 * 1024:
                raise ValueError
            valid = True
        except Exception:
            pass
        if not valid:
            raise _invalid_registration() from None
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "input_schema", frozen_schema)


@dataclass(frozen=True)
class Tool:
    id: str
    name: str
    description: str
    capability: str
    governed: bool
    connector: str | None
    action: str | None
    created_at: str
    current_version_id: str | None = None


@dataclass(frozen=True)
class ToolVersion:
    id: str
    tool_id: str
    version: str
    content_digest: str
    input_schema: Mapping[str, Any]
    tool_contract_digest: str | None
    created_at: str
    intent: str | None = None
    intent_drafting: Mapping[str, Any] | None = None
    governance_revision_id: str | None = None

    def __post_init__(self):
        if self.governance_revision_id is not None:
            _validate_catalog_uuid(self.governance_revision_id)
        schema = _freeze_catalog_json(thaw_json(dict(self.input_schema)))
        _validate_schema(schema)
        if self.tool_contract_digest is not None and (
            not isinstance(self.tool_contract_digest, str)
            or not _DIGEST.fullmatch(self.tool_contract_digest)
        ):
            raise _invalid_registration()
        if self.intent is not None and (
            type(self.intent) is not str
            or not self.intent.strip()
            or "\0" in self.intent
            or _utf8_length(self.intent) > 2000
        ):
            raise _invalid_registration()
        if self.intent_drafting is not None:
            object.__setattr__(
                self,
                "intent_drafting",
                _freeze_catalog_json(thaw_json(dict(self.intent_drafting))),
            )
        object.__setattr__(self, "input_schema", schema)


@dataclass(frozen=True)
class PolicyInputRequirement:
    binding_id: str
    policy_id: str
    policy_version_id: str
    input_digest: str
    field_selectors: tuple[FieldSelector, ...]

    def __post_init__(self):
        for value in (self.binding_id, self.policy_id, self.policy_version_id):
            _validate_catalog_uuid(value)
        if not isinstance(self.input_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.input_digest
        ):
            raise _invalid_registration()
        selectors = tuple(self.field_selectors)
        if not all(isinstance(item, FieldSelector) for item in selectors):
            raise _invalid_registration()
        _validate_selectors(selectors, None)
        object.__setattr__(self, "field_selectors", selectors)


@dataclass(frozen=True)
class ToolReadiness:
    """Server policy requirements for an exact ToolVersion; registration is not permission."""

    request_index: int | None
    name: str
    tool_id: str | None
    tool_version_id: str | None
    registration_status: str
    governed: bool
    policy_ready: bool
    eligible: bool
    reason: str
    policy_contract_set_digest: str | None = None
    policy_input_contracts: tuple[PolicyInputRequirement, ...] = ()
    governance_revision_id: str | None = None

    def __post_init__(self):
        if self.governance_revision_id is not None:
            _validate_catalog_uuid(self.governance_revision_id)
        requirements = tuple(self.policy_input_contracts)
        if len(requirements) > 32 or not all(
            isinstance(item, PolicyInputRequirement) for item in requirements
        ):
            raise _invalid_registration()
        if len({item.binding_id for item in requirements}) != len(requirements):
            raise _invalid_registration()
        digest = self.policy_contract_set_digest
        if digest is not None:
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise _invalid_registration()
            if (
                not self.eligible
                or not self.policy_ready
                or self.reason != "ready"
                or not self.governed
            ):
                raise _invalid_registration()
            _validate_catalog_uuid(self.tool_id)
            _validate_catalog_uuid(self.tool_version_id)
        elif requirements:
            raise _invalid_registration()
        object.__setattr__(self, "policy_input_contracts", requirements)


def collect_selected_values(
    arguments: Mapping[str, JSONInput], readiness: ToolReadiness
) -> tuple[dict[str, JSONInput], ...]:
    """Copy the minimal union of native paths required by active policies."""
    if (
        not isinstance(arguments, Mapping)
        or readiness.policy_contract_set_digest is None
    ):
        raise ConfigurationError("invalid_input")
    paths = sorted(
        {
            field.path
            for item in readiness.policy_input_contracts
            for field in item.field_selectors
        }
    )
    selected: list[dict[str, JSONInput]] = []
    covered: list[str] = []
    for path in paths:
        if any(path.startswith(parent + ".") for parent in covered):
            continue
        covered.append(path)
        value = arguments
        for part in path.split("."):
            if not isinstance(value, Mapping) or part not in value:
                break
            value = value[part]
        else:
            try:
                selected.append({"path": path, "value": copy_json_value(value)})
            except Exception:
                raise ConfigurationError("invalid_input") from None
    # Apply the same integer, size, nesting, and cycle checks as execution inputs.
    return tuple(json.loads(snapshot_json(selected, "invalid_input")))


@dataclass(frozen=True)
class RegistrationResult:
    """Per-item publication result correlated to the original request index."""

    request_index: int
    status: str
    reason: str
    tool: Tool | None
    tool_version: ToolVersion | None
    readiness: ToolReadiness | None


@dataclass(frozen=True)
class AgentReadiness:
    """Registration and policy readiness for the requested Agent catalog selection."""

    tools: tuple[ToolReadiness, ...]
    fully_registered: bool
    registration_reason: str
    fully_policy_ready: bool
    policy_reason: str


def _parse_tool(data: Any) -> Tool:
    if not isinstance(data, dict):
        raise _invalid_registration()
    if type(data.get("governed")) is not bool:
        raise _invalid_registration()
    if (
        not isinstance(data.get("name"), str)
        or not isinstance(data.get("description"), str)
        or data.get("capability") not in _CAPABILITIES
    ):
        raise _invalid_registration()
    for key in ("connector", "action"):
        if data.get(key) is not None and not isinstance(data[key], str):
            raise _invalid_registration()
    return Tool(
        _validate_catalog_uuid(data.get("id")),
        data["name"],
        data["description"],
        data["capability"],
        data["governed"],
        data.get("connector") or None,
        data.get("action") or None,
        _validate_catalog_timestamp(data["created_at"]),
        _optional_uuid(data.get("current_version_id")),
    )


def _parse_tool_version(data: Any) -> ToolVersion:
    if not isinstance(data, dict):
        raise _invalid_registration()
    if not isinstance(data.get("input_schema_json"), str) or any(
        key in data for key in ("field_selectors", "policy_input_digest")
    ):
        raise _invalid_registration()
    try:
        schema = json.loads(data["input_schema_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        raise _invalid_registration() from None
    _validate_schema(schema)
    digest = data["content_digest"]
    if (
        not isinstance(data.get("version"), str)
        or not isinstance(digest, str)
        or not _DIGEST.fullmatch(digest)
    ):
        raise _invalid_registration()
    native = data.get("tool_contract_digest")
    if native is not None and (
        not isinstance(native, str) or not _DIGEST.fullmatch(native)
    ):
        raise _invalid_registration()
    intent = data.get("intent")
    drafting = data.get("intent_drafting")
    if intent is not None and (
        type(intent) is not str
        or not intent.strip()
        or "\0" in intent
        or _utf8_length(intent) > 2000
    ):
        raise _invalid_registration()
    if drafting is not None:
        if not isinstance(drafting, dict) or drafting.get("status") not in {
            "not_required",
            "waiting_classification",
            "queued",
            "running",
            "failed",
            "complete",
        }:
            raise _invalid_registration()
        drafting = _freeze_catalog_json(drafting)
    return ToolVersion(
        _validate_catalog_uuid(data["id"]),
        _validate_catalog_uuid(data["tool_id"]),
        data["version"],
        digest,
        _freeze_catalog_json(schema),
        native,
        _validate_catalog_timestamp(data["created_at"]),
        intent,
        drafting,
        _optional_uuid(data.get("governance_revision_id")),
    )


def parse_readiness(data: Any) -> ToolReadiness:
    if not isinstance(data, dict) or data.get("registration_status") not in _STATUSES:
        raise _invalid_registration()
    if (
        type(data.get("governed")) is not bool
        or type(data.get("policy_ready")) is not bool
        or type(data.get("eligible")) is not bool
        or not isinstance(data.get("name"), str)
        or not isinstance(data.get("reason"), str)
    ):
        raise _invalid_registration()
    requirements = data.get("policy_input_contracts", [])
    if not isinstance(requirements, list):
        raise _invalid_registration()
    parsed = []
    for item in requirements:
        if not isinstance(item, dict) or not isinstance(
            item.get("field_selectors"), list
        ):
            raise _invalid_registration()
        fields = []
        for field in item["field_selectors"]:
            if (
                not isinstance(field, dict)
                or set(field) - {"path", "alias"}
                or "path" not in field
            ):
                raise _invalid_registration()
            fields.append(FieldSelector(field["path"], field.get("alias")))
        parsed.append(
            PolicyInputRequirement(
                item.get("binding_id"),
                item.get("policy_id"),
                item.get("policy_version_id"),
                item.get("input_digest"),
                tuple(fields),
            )
        )
    return ToolReadiness(
        data.get("request_index"),
        data["name"],
        _optional_uuid(data.get("tool_id")),
        _optional_uuid(data.get("tool_version_id")),
        data["registration_status"],
        data["governed"],
        data["policy_ready"],
        data["eligible"],
        data["reason"],
        data.get("policy_contract_set_digest"),
        tuple(parsed),
        data.get("governance_revision_id"),
    )


def parse_registration(data: Any) -> RegistrationResult:
    if not isinstance(data, dict) or data.get("status") not in {
        "registered",
        "replayed",
        "invalid",
        "conflict",
    }:
        raise _invalid_registration()
    status = data["status"]
    if (
        type(data.get("request_index")) is not int
        or data["request_index"] < 0
        or not isinstance(data.get("reason"), str)
    ):
        raise _invalid_registration()
    if isinstance(data.get("readiness"), dict) and any(
        key in data["readiness"]
        for key in ("policy_contract_set_digest", "policy_input_contracts")
    ):
        raise _invalid_registration()
    return RegistrationResult(
        data["request_index"],
        status,
        data["reason"],
        _parse_tool(data["tool"]) if status in {"registered", "replayed"} else None,
        _parse_tool_version(data["tool_version"])
        if status in {"registered", "replayed"}
        else None,
        parse_readiness(data["readiness"])
        if status in {"registered", "replayed"}
        else None,
    )


def _validate_schema(schema: Any) -> None:
    if not isinstance(schema, Mapping):
        raise _invalid_registration()
    if "$schema" in schema and schema["$schema"] != _DIALECT:
        raise _invalid_registration()
    if schema.get("type") not in (None, "object"):
        raise _invalid_registration()
    defs = schema.get("$defs", {})
    if not isinstance(defs, Mapping):
        raise _invalid_registration()
    nodes = 0

    def walk(value: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if depth > 32 or nodes > 4096:
            raise _invalid_registration()
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key in {"$id", "$anchor", "$dynamicAnchor", "$dynamicRef"}:
                    raise _invalid_registration()
                if key == "$ref":
                    if not isinstance(child, str) or not child.startswith("#/$defs/"):
                        raise _invalid_registration()
                    name = (
                        child.removeprefix("#/$defs/")
                        .replace("~1", "/")
                        .replace("~0", "~")
                    )
                    if name not in defs:
                        raise _invalid_registration()
                if (
                    key == "properties"
                    and isinstance(child, Mapping)
                    and any("." in name for name in child)
                ):
                    raise _invalid_registration()
                if key in {"pattern", "patternProperties"} and isinstance(child, str):
                    if "(?" in child or re.search(r"(?:\\[1-9]|\\g<|\\k<)", child):
                        raise _invalid_registration()
                    try:
                        re.compile(child)
                    except re.error:
                        raise _invalid_registration() from None
                if key == "patternProperties" and isinstance(child, Mapping):
                    for pattern in child:
                        if (
                            not isinstance(pattern, str)
                            or "(?" in pattern
                            or re.search(r"(?:\\[1-9]|\\g<|\\k<)", pattern)
                        ):
                            raise _invalid_registration()
                        try:
                            re.compile(pattern)
                        except re.error:
                            raise _invalid_registration() from None
                walk(child, depth + 1)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child, depth + 1)

    walk(schema, 0)


def _validate_selectors(
    selectors: Sequence[FieldSelector], schema: Mapping[str, Any] | None
) -> None:
    if len(selectors) > 64:
        raise _invalid_registration()
    seen_source: set[str] = set()
    seen_output: set[str] = set()
    for selector in selectors:
        output = selector.alias or selector.path
        if selector.path in seen_source or output in seen_output:
            raise _invalid_registration()
        seen_source.add(selector.path)
        seen_output.add(output)
        if schema is not None:
            _resolve_selector(schema, selector.path.split("."))
    for left in seen_output:
        for right in seen_output:
            if right.startswith(left + "."):
                raise _invalid_registration()


def _resolve_selector(schema: Mapping[str, Any], parts: list[str]) -> None:
    defs = schema.get("$defs", {})
    current: Any = schema.get("properties", {})
    for index, part in enumerate(parts):
        if not isinstance(current, Mapping) or part not in current:
            raise _invalid_registration()
        current = current[part]
        hops: set[str] = set()
        while isinstance(current, Mapping) and "$ref" in current:
            ref = current["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
                raise _invalid_registration()
            name = ref.removeprefix("#/$defs/").replace("~1", "/").replace("~0", "~")
            if name in hops or name not in defs:
                raise _invalid_registration()
            hops.add(name)
            if len(hops) > 32:
                raise _invalid_registration()
            current = defs[name]
        if index < len(parts) - 1:
            if not isinstance(current, Mapping) or current.get("type") == "array":
                raise _invalid_registration()
            if any(key in current for key in ("oneOf", "anyOf", "allOf")):
                raise _invalid_registration()
            current = current.get("properties", {})
