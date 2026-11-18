"""Build and publish one atomic generation while retaining each executable graph."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from openeight._framework._types import ManagedBuild
from openeight.errors import ConfigurationError

STATIC_KEY = "__static__"


@dataclass(frozen=True)
class VariantGeneration:
    generation: int
    builds: Mapping[str, ManagedBuild]
    catalog_facts: tuple[Any, ...]
    binding_maps: Mapping[str, Mapping[str, Any]]


def assemble_variant_generation(
    build, *, scoped, variant_names, resources, governance, remotes, generation
):
    if (
        not variant_names
        or len(set(variant_names)) != len(variant_names)
        or None in variant_names
        and variant_names != (None,)
    ):
        raise ConfigurationError("invalid_integration_configuration")
    builds, facts, canonical = {}, {}, {}
    for name in variant_names:
        current = build(
            scoped,
            remotes,
            resources=resources,
            governance=governance,
            variant_name=name,
        )
        if not isinstance(current, ManagedBuild):
            raise ConfigurationError("unsupported_graph_shape")
        current.bindings.validate()
        builds[STATIC_KEY if name is None else name] = current
        for catalog_name, binding in current.bindings.entries.items():
            prior = facts.get(catalog_name)
            if prior is not None and prior != binding.facts:
                raise ConfigurationError("conflicting_tool_definitions")
            facts[catalog_name] = binding.facts
            canonical.setdefault(catalog_name, binding)
    max_tools = governance.discovery.max_tools if governance.discovery else 200
    max_bytes = (
        governance.discovery.max_catalog_bytes if governance.discovery else 1048576
    )
    if (
        len(facts) > max_tools
        or sum(
            len(binding.schema_bytes)
            + len(binding.facts.name.encode())
            + len(binding.facts.description.encode())
            for binding in canonical.values()
        )
        > max_bytes
    ):
        raise ConfigurationError("tool_discovery_limit_exceeded")
    return VariantGeneration(
        generation,
        MappingProxyType(builds),
        tuple(facts.values()),
        MappingProxyType(
            {
                name: MappingProxyType(
                    {
                        binding.facts.name: canonical[binding.facts.name]
                        for binding in current.bindings.native.values()
                    }
                )
                for name, current in builds.items()
            }
        ),
    )


class VariantCatalog:
    """Publication-only union; invocations resolve through their selected build."""

    def __init__(self, generation):
        self.generation = generation
        entries, native = {}, {}
        for build in generation.builds.values():
            for name, binding in build.bindings.entries.items():
                entries.setdefault(name, binding)
            for key, binding in build.bindings.native.items():
                native.setdefault(key, binding)
        self.entries, self.native = MappingProxyType(entries), MappingProxyType(native)
        self.facts = generation.catalog_facts
        self.completeness, self.diagnostic = "complete", None

    def validate(self):
        for build in self.generation.builds.values():
            build.bindings.validate()

    def resolve(self, path, name):
        raise ConfigurationError("unsupported_graph_shape")

    def snapshot(self):
        records, seen = [], set()
        for build in self.generation.builds.values():
            for item in build.bindings.snapshot():
                key = (item["child_path"], item["native_name"], item["name"])
                if key not in seen:
                    records.append(item)
                    seen.add(key)
        return tuple(records)
