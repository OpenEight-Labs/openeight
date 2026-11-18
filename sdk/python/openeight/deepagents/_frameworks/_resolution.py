"""Session-local native Deep model resolution, retaining harness provenance."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping

from langchain_core.language_models import BaseChatModel

from openeight._framework._diagnostics import safe_resolution_diagnostics
from openeight._framework._measurement import payload_hash
from openeight._framework._runtime_models import (
    capture_model,
    normalize_captured,
    resolve_captured,
)

from ._inspection import profile_for


@dataclass(frozen=True)
class ResolvedModel:
    original_model_string: str | None
    model: BaseChatModel
    profile: object | None
    source: Literal["explicit", "inherited", "root-general-purpose", "nested"]
    configuration_sha256: str | None = None


@dataclass(frozen=True)
class ResolvedTopology:
    nodes: Mapping[tuple[str, ...], ResolvedModel]


def capture_declarations(model, children, *, enrolled_keys=()):
    from deepagents.middleware.subagents import SubAgentMiddleware

    from . import _nested

    captured = {
        (): (*capture_model(model, enrolled_keys=enrolled_keys, deep=True), "explicit")
    }

    def visit(spec, path, source):
        if "model" in spec:
            captured[path] = (
                *capture_model(spec["model"], enrolled_keys=enrolled_keys, deep=True),
                source,
            )
        else:
            captured[path] = (*captured[()][:3], "inherited")
        for item in spec.get("middleware", ()):
            if type(item) is SubAgentMiddleware:
                for child in _nested.validate(item):
                    visit(child, (*path, child["name"]), "nested")

    for spec in children:
        visit(spec, (spec["name"],), "explicit")
    return MappingProxyType(captured)


def normalized_declarations(captured, selectors):
    from openeight.deepagents._deployment import canonical

    return [
        {"path": list(path), "configuration": normalize_captured(entry[0], selectors)}
        for path, entry in sorted(
            captured.items(), key=lambda pair: canonical(list(pair[0])).encode()
        )
    ]


async def resolve_topology(request) -> ResolvedTopology:
    safe_resolution_diagnostics()
    from deepagents.profiles.harness.harness_profiles import _harness_profile_for_model

    captured = request.options["captured_models"]
    cache = request.options["resolution_cache"]
    selectors = request.options["model_selectors"]
    nodes = {}

    def resolve(path):
        normalize, construct, _, source = captured[path]
        digest = payload_hash(normalize_captured(normalize, selectors))
        key = path, digest
        if key in cache:
            return cache[key]
        if source == "inherited":
            actual, original = nodes[()].model, None
        else:
            actual, original = resolve_captured(construct, selectors)
        profile = (
            None
            if source == "nested"
            else profile_for(
                actual,
                original_model_string=original,
                resolve_profile=_harness_profile_for_model,
            )
        )
        record = ResolvedModel(
            original,
            actual,
            profile,
            source,
            digest if request.variant_name is not None else None,
        )
        cache[key] = record
        return record

    nodes[()] = resolve(())
    for path in captured:
        if path:
            nodes[path] = resolve(path)
    root = nodes[()]
    gp = root.profile.general_purpose_subagent
    enabled = request.options["include_general_purpose"]
    if enabled is None:
        enabled = gp is None or gp.enabled is not False
    if enabled and ("general-purpose",) not in nodes:
        nodes[("general-purpose",)] = ResolvedModel(
            root.original_model_string,
            root.model,
            root.profile,
            "root-general-purpose",
            root.configuration_sha256,
        )
    return ResolvedTopology(MappingProxyType(nodes))
