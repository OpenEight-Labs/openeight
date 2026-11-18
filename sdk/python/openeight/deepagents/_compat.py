"""Read-only validation of the pinned Deep Agents execution topology."""

from importlib.metadata import version

from deepagents.profiles.harness.harness_profiles import _harness_profile_for_model

from openeight._framework import detect_framework, select_compatibility
from openeight._framework._registry import load_registry

PINS = next(
    row["packages"]
    for row in load_registry()["entries"]
    if row["id"] == "deepagents-01"
)


def validate_versions():
    from openeight.langchain._compat import validate_versions as validate_dependencies

    validate_dependencies()
    return select_compatibility(
        detect_framework("deepagents", version=version),
        required=frozenset({"ordinary"}),
    )


def profile_for(model):
    from ._frameworks._inspection import profile_for as profile

    return profile(model, resolve_profile=_harness_profile_for_model)


def capture(graph, boundaries, surfaces=None):
    from ._frameworks._inspection import capture as inspect

    return inspect(graph, boundaries, surfaces)
