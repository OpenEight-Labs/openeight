"""Compatibility aliases for the SDK qualification registry."""

from importlib.metadata import version

from openeight._framework import detect_framework, select_compatibility
from openeight._framework._registry import load_registry

PACKAGES = (
    "langchain",
    "langchain-core",
    "langgraph",
    "langgraph-prebuilt",
    "langgraph-checkpoint",
)
CLOSURES = frozenset(
    tuple(row["packages"][name] for name in PACKAGES)
    for row in load_registry()["entries"]
    if row["integration"] == "langchain" and ["ordinary"] in row["feature_sets"]
)


def validate_managed_versions():
    return select_compatibility(
        detect_framework("langchain", version=version), required=frozenset({"ordinary"})
    )
