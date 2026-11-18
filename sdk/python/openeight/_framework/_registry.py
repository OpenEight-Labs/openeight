"""Exact, offline framework and feature-combination selection."""

import json
import sys
import sysconfig
from dataclasses import dataclass
from importlib import metadata, resources
from types import MappingProxyType
from typing import Literal, Mapping

from openeight.errors import ConfigurationError

from ._schema import _IMPLEMENTATIONS, _PACKAGES, _unique_object, validate_registry

ExecutionFeature = Literal[
    "ordinary",
    "sync",
    "stream",
    "checkpoint",
    "mcp",
    "durable",
    "local_durable",
    "managed_integrations",
]


@dataclass(frozen=True)
class InstalledFramework:
    integration: Literal["langchain", "deepagents"]
    python: str
    packages: Mapping[str, str]

    def __post_init__(self):
        object.__setattr__(self, "packages", MappingProxyType(dict(self.packages)))


@dataclass(frozen=True)
class CompatibilitySelection:
    qualification_id: str
    implementation_id: str
    implementation_revision: int
    supported_features: frozenset[ExecutionFeature]
    checkpoint_format: str | None
    installed: InstalledFramework


def load_registry():
    try:
        data = (
            resources.files(__package__).joinpath("frameworks.json").read_text("utf-8")
        )
        return validate_registry(json.loads(data, object_pairs_hook=_unique_object))
    except (ValueError, TypeError, KeyError, OSError):
        raise ConfigurationError("unsupported_framework_version") from None


def detect_framework(integration, *, version=metadata.version):
    if (
        integration not in _IMPLEMENTATIONS
        or sys.implementation.name != "cpython"
        or sys.version_info.releaselevel != "final"
        or sysconfig.get_config_var("Py_GIL_DISABLED")
    ):
        raise ConfigurationError("unsupported_framework_version")
    names = (
        *_PACKAGES,
        *(("deepagents",) if integration == "deepagents" else ()),
        "langgraph-checkpoint-sqlite",
        "aiosqlite",
    )
    packages = {}
    for name in names:
        try:
            packages[name] = version(name)
        except metadata.PackageNotFoundError:
            pass
    return InstalledFramework(
        integration, f"{sys.version_info.major}.{sys.version_info.minor}", packages
    )


def select_compatibility(installed, *, required):
    rows = load_registry()["entries"]
    matches = [
        row
        for row in rows
        if row["integration"] == installed.integration
        and installed.python in row["python"]
        and all(installed.packages.get(k) == v for k, v in row["packages"].items())
    ]
    if (
        matches
        and required & {"durable", "local_durable"}
        and not {"langgraph-checkpoint-sqlite", "aiosqlite"} <= set(installed.packages)
    ):
        raise ConfigurationError("durable_dependencies_missing")
    selected = [row for row in matches if sorted(required) in row["feature_sets"]]
    if len(selected) != 1:
        if len(selected) > 1:
            raise ConfigurationError("unsupported_framework_version")
        if matches:
            raise ConfigurationError("unsupported_framework_feature")
        raise ConfigurationError("unsupported_framework_version")
    row = selected[0]
    return CompatibilitySelection(
        row["id"],
        row["implementation_id"],
        row["implementation_revision"],
        frozenset(required),
        row["checkpoint_format"],
        installed,
    )
