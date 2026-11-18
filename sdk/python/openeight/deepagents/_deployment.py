"""Canonical deployment identity and read-only execution contract records."""

import hashlib
import json
import re
from dataclasses import dataclass
from uuid import UUID

from openeight._credential_binding import credential_binding as credential_binding
from openeight.client import _validate_agent_name, _validate_base_url
from openeight.errors import ConfigurationError
from openeight.tools import ToolReadiness

MAX_MANIFEST_BYTES = 1024 * 1024
IDENTITY_KEYS = {
    "kind",
    "endpoint",
    "agent_name",
    "organization_id",
    "workspace_id",
    "agent_id",
    "api_key_id",
}


@dataclass(frozen=True)
class BoundExecutionContract:
    child_path: tuple[str, ...]
    native_name: str
    catalog_name: str
    tool_id: str
    tool_version_id: str
    input_schema_json: str
    recovery_kind: str
    readiness: ToolReadiness


@dataclass(frozen=True)
class BoundCatalogSnapshot:
    contracts: tuple[BoundExecutionContract, ...]


def _fail():
    raise ConfigurationError("durable_deployment_mismatch") from None


def canonical(value):
    def validate(item):
        if item is None or type(item) in {bool, int}:
            return
        if type(item) is str:
            item.encode("utf-8")
            return
        if type(item) is list:
            for child in item:
                validate(child)
            return
        if type(item) is dict and all(type(k) is str for k in item):
            for key, child in item.items():
                key.encode("utf-8")
                validate(child)
            return
        raise ValueError

    try:
        validate(value)
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(raw.encode()) > MAX_MANIFEST_BYTES:
            raise ValueError
        return raw
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail()


def parse(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError
            value[key] = item
        return value

    try:
        if type(raw) is not str or len(raw.encode()) > MAX_MANIFEST_BYTES:
            raise ValueError
        value = json.loads(raw, object_pairs_hook=unique)
        if canonical(value) != raw:
            raise ValueError
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail()


def uuid(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        _fail()


def digest(value):
    if type(value) is not str or not re.fullmatch("[0-9a-f]{64}", value):
        _fail()


def identity(raw):
    value = parse(raw)
    if type(value) is not dict or set(value) != IDENTITY_KEYS:
        _fail()
    try:
        if _validate_base_url(value["endpoint"]) != value["endpoint"]:
            _fail()
        _validate_agent_name(value["agent_name"])
    except ConfigurationError:
        _fail()
    ids = [
        value[key]
        for key in ("organization_id", "workspace_id", "agent_id", "api_key_id")
    ]
    if value["kind"] == "credential_bound":
        if any(v is not None for v in ids):
            _fail()
    elif value["kind"] == "verified_issuance":
        for item in ids:
            uuid(item)
    else:
        _fail()
    return value


def _validate_manifest(raw, *, bound=True):
    value = parse(raw)
    keys = {
        "format_version",
        "integration",
        "deployment_id",
        "identity",
        "application",
        "configuration_sha256",
        "packages",
        "backend",
        "saver",
        "compatibility",
        "catalog_naming",
        "topology",
        "effective_configuration",
        "effective_configuration_sha256",
    }
    if type(value) is not dict or set(value) != keys:
        _fail()
    if type(value["format_version"]) is not int or value["format_version"] != 2:
        _fail()
    if value["integration"] != "deepagents":
        _fail()
    uuid(value["deployment_id"])
    identity(canonical(value["identity"]))
    application = value["application"]
    if type(application) is not dict or set(application) != {"kind", "value"}:
        _fail()
    if application["kind"] == "artifact_sha256":
        digest(application["value"])
    elif application["kind"] == "revision":
        from .durable import LocalDurability

        try:
            LocalDurability(directory=".", revision=application["value"])
        except ConfigurationError:
            _fail()
    else:
        _fail()
    digest(value["configuration_sha256"])
    packages = value["packages"]
    expected = {
        "openeight",
        "deepagents",
        "langchain",
        "langchain-core",
        "langgraph",
        "langgraph-prebuilt",
        "langgraph-checkpoint",
        "langgraph-checkpoint-sqlite",
        "aiosqlite",
        "python",
    }
    if type(packages) is not dict or set(packages) != expected:
        _fail()
    if any(
        type(v) is not str or not v or len(v.encode()) > 128 for v in packages.values()
    ):
        _fail()
    if (
        value["backend"] != {"kind": "state", "format_version": 1}
        or type(value["backend"]["format_version"]) is not int
    ):
        _fail()
    if (
        value["saver"]
        != {"kind": "async-sqlite", "format_version": 1, "pickle_fallback": False}
        or type(value["saver"]["format_version"]) is not int
        or type(value["saver"]["pickle_fallback"]) is not bool
    ):
        _fail()
    compatibility = value["compatibility"]
    if (
        type(compatibility) is not dict
        or set(compatibility)
        != {
            "implementation_id",
            "implementation_revision",
            "qualification_id",
            "checkpoint_format",
        }
        or type(compatibility["implementation_revision"]) is not int
        or compatibility["implementation_revision"] < 1
        or any(
            type(compatibility[k]) is not str or not compatibility[k]
            for k in ("implementation_id", "qualification_id", "checkpoint_format")
        )
    ):
        _fail()
    if value["catalog_naming"] not in {"hashed-v1", "qualified-v1"}:
        _fail()
    topology = value["topology"]
    if type(topology) is not list or not 1 <= len(topology) <= 200:
        _fail()
    paths = set()
    count = 0
    catalog_names, tool_ids, version_ids = set(), set(), set()
    for node in topology:
        if type(node) is not dict or set(node) != {"path", "delegates_to", "tools"}:
            _fail()
        path = node["path"]
        if (
            type(path) is not list
            or len(path) > 16
            or any(type(v) is not str or not v or len(v.encode()) > 256 for v in path)
        ):
            _fail()
        path = tuple(path)
        if path in paths:
            _fail()
        paths.add(path)
        names = set()
        tools = node["tools"]
        if type(tools) is not list:
            _fail()
        count += len(tools)
        for tool in tools:
            tool_keys = {"native_name", "catalog_name", "input_schema_sha256"}
            if bound:
                tool_keys |= {"tool_id", "tool_version_id", "recovery_kind"}
            if type(tool) is not dict or set(tool) != tool_keys:
                _fail()
            for key in ("native_name", "catalog_name"):
                text = tool[key]
                if type(text) is not str or not text or len(text.encode()) > 256:
                    _fail()
            if tool["native_name"] in names:
                _fail()
            names.add(tool["native_name"])
            if tool["catalog_name"] in catalog_names:
                _fail()
            catalog_names.add(tool["catalog_name"])
            digest(tool["input_schema_sha256"])
            if bound:
                if (
                    tool["tool_id"] in tool_ids
                    or tool["tool_version_id"] in version_ids
                ):
                    _fail()
                tool_ids.add(tool["tool_id"])
                version_ids.add(tool["tool_version_id"])
                uuid(tool["tool_id"])
                uuid(tool["tool_version_id"])
                if tool["recovery_kind"] not in {
                    "read",
                    "business_write",
                    "state_write",
                    "delegation",
                }:
                    _fail()
        if tools != sorted(tools, key=lambda t: t["native_name"].encode()):
            _fail()
    if () not in paths or count > 200:
        _fail()
    if topology != sorted(topology, key=lambda n: canonical(n["path"]).encode()):
        _fail()
    for node in topology:
        targets = node["delegates_to"]
        if type(targets) is not list or any(
            type(p) is not list or tuple(p) not in paths for p in targets
        ):
            _fail()
        if targets != sorted(targets, key=lambda p: canonical(p).encode()) or len(
            {tuple(p) for p in targets}
        ) != len(targets):
            _fail()
        if list(node["path"]) in targets:
            _fail()
    edges = {
        tuple(node["path"]): [tuple(p) for p in node["delegates_to"]]
        for node in topology
    }
    visiting, done = set(), set()

    def visit(path):
        if path in visiting:
            _fail()
        if path in done:
            return
        visiting.add(path)
        for target in edges[path]:
            visit(target)
        visiting.remove(path)
        done.add(path)

    for path in paths:
        visit(path)
    _effective_configuration(value)
    return value


def _effective_configuration(manifest):
    descriptor = manifest["effective_configuration"]
    digest(manifest["effective_configuration_sha256"])
    if type(descriptor) is not dict or set(descriptor) != {
        "format_version",
        "nodes",
        "catalog_naming",
        "backend",
        "saver",
    }:
        _fail()
    if (
        type(descriptor["format_version"]) is not int
        or descriptor["format_version"] != 2
    ):
        _fail()
    for field in ("catalog_naming", "backend", "saver"):
        if descriptor[field] != manifest[field]:
            _fail()
    if (
        hashlib.sha256(canonical(descriptor).encode()).hexdigest()
        != manifest["effective_configuration_sha256"]
    ):
        _fail()
    nodes = descriptor["nodes"]
    if type(nodes) is not list or len(nodes) != len(manifest["topology"]):
        _fail()
    expected_keys = {
        "path",
        "source",
        "model_class",
        "model_spec_sha256",
        "system_message_sha256",
        "middleware",
        "tools",
        "delegates_to",
        "state_schema_kind",
        "private_state_keys",
        "default_config_sha256",
        "runtime_variant",
        "response_format_sha256",
    }
    for node, topology in zip(nodes, manifest["topology"]):
        if type(node) is not dict or set(node) != expected_keys:
            _fail()
        if (
            node["path"] != topology["path"]
            or node["delegates_to"] != topology["delegates_to"]
            or node["source"]
            not in {"explicit", "inherited", "root-general-purpose", "nested"}
        ):
            _fail()
        for child in node["delegates_to"]:
            if len(child) != len(node["path"]) + 1 or child[:-1] != node["path"]:
                _fail()
        for field in ("model_class", "state_schema_kind"):
            if (
                type(node[field]) is not str
                or not node[field]
                or len(node[field].encode()) > 1024
            ):
                _fail()
        for field in ("system_message_sha256", "default_config_sha256"):
            digest(node[field])
        for field in ("model_spec_sha256", "response_format_sha256"):
            if node[field] is not None:
                digest(node[field])
        variant = node["runtime_variant"]
        if variant is not None:
            if (
                type(variant) is not dict
                or set(variant) != {"name", "configuration_sha256"}
                or type(variant["name"]) is not str
                or not variant["name"]
            ):
                _fail()
            digest(variant["configuration_sha256"])
        private = node["private_state_keys"]
        if (
            type(private) is not list
            or any(type(key) is not str or not key for key in private)
            or private != sorted(set(private))
        ):
            _fail()
        middleware = node["middleware"]
        if type(middleware) is not list:
            _fail()
        for item in middleware:
            if (
                type(item) is not dict
                or set(item) != {"kind", "configuration_sha256"}
                or type(item["kind"]) is not str
                or not item["kind"]
            ):
                _fail()
            digest(item["configuration_sha256"])
        tools = node["tools"]
        if type(tools) is not list or len(tools) != len(topology["tools"]):
            _fail()
        for tool, expected in zip(tools, topology["tools"]):
            if type(tool) is not dict or set(tool) != {
                "native_name",
                "catalog_name",
                "input_schema_sha256",
                "executable_description_sha256",
                "model_visible_description_sha256",
            }:
                _fail()
            if any(
                tool[key] != expected[key]
                for key in ("native_name", "catalog_name", "input_schema_sha256")
            ):
                _fail()
            for key in (
                "input_schema_sha256",
                "executable_description_sha256",
                "model_visible_description_sha256",
            ):
                digest(tool[key])
    variants = [node["runtime_variant"] for node in nodes]
    if any(value is not None for value in variants) and (
        any(value is None for value in variants)
        or len({value["name"] for value in variants}) != 1
    ):
        _fail()


def manifest(raw):
    try:
        return _validate_manifest(raw)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        _fail()


def unbound_manifest(raw):
    """Validate measured local facts before any initial catalog publication."""
    try:
        return _validate_manifest(raw, bound=False)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        _fail()
