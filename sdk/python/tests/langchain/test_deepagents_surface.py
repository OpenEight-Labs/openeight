"""Explicit native surfaces preserve dispatch identity and fail closed."""

from dataclasses import replace
from inspect import getclosurevars

import pytest
from langchain_core.messages import AIMessage
from test_managed_deepagents import PolicyClient, call, config, effect, model

from openeight import ConfigurationError
from openeight.deepagents import CatalogDescription, ToolSurface, create_deep_agent
from openeight.langchain import _owner
from openeight.langchain._bindings import mapped_name

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def client(monkeypatch):
    PolicyClient.instances = []
    monkeypatch.setattr(_owner, "Client", PolicyClient)


def options(effects, **overrides):
    value = dict(
        tools=[],
        openeight=config(on_block="raise"),
        catalog_naming="qualified-v1",
        include_general_purpose=False,
        subagents=[
            {
                "name": "worker",
                "description": "Do one task",
                "model": model(call()),
                "tools": [effect(effects)],
            },
            {
                "name": "reporting",
                "description": "Report",
                "model": model(AIMessage(content="done")),
                "tools": [],
            },
        ],
        tool_surfaces=(
            ToolSurface((), ("read_file", "write_file", "task")),
            ToolSurface(("worker",), ("effect",)),
            ToolSurface(("reporting",), ()),
        ),
    )
    value.update(overrides)
    return value


async def test_exact_executor_catalog_names_and_native_descriptions():
    effects = []
    native = effect(effects).description
    async with create_deep_agent(
        model(AIMessage(content="done")),
        **options(
            effects,
            catalog_descriptions=(
                CatalogDescription(("worker",), "effect", "Perform one test effect."),
            ),
        ),
    ) as owner:
        graph = owner._session._owner._graph
        root = graph.nodes["tools"].bound.tools_by_name
        assert set(root) == {"read_file", "write_file", "task"}
        children = getclosurevars(root["task"].coroutine).nonlocals["subagent_graphs"]
        assert set(children) == {"worker", "reporting"}
        assert set(children["worker"].nodes["tools"].bound.tools_by_name) == {"effect"}
        assert children["reporting"].nodes["tools"].bound.tools_by_name == {}
        assert (
            children["worker"].nodes["tools"].bound.tools_by_name["effect"].description
            == native
        )
        facts = owner.catalog_snapshot().facts
        assert {f.name for f in facts} == {
            "coordinator.read_file",
            "coordinator.write_file",
            "coordinator.task",
            "worker.effect",
        }
        assert (
            next(f.description for f in facts if f.name == "worker.effect")
            == "Perform one test effect."
        )


async def test_child_denial_survives_allowed_delegation():
    effects = []
    task = call("task", description="run", subagent_type="worker")
    # task's native schema has no value field.
    async with create_deep_agent(model(task), **options(effects)) as owner:
        await owner.aflush()
        PolicyClient.instances[-1].reject_name = "worker.effect"
        from openeight import BlockedError

        with pytest.raises(BlockedError):
            await owner.ainvoke({"messages": [("user", "run")]})
    assert effects == []


@pytest.mark.parametrize(
    "name", ["ls", "edit_file", "delete", "glob", "grep", "execute", "effect"]
)
async def test_forged_excluded_root_call_has_no_effect(name):
    effects = []
    forged = AIMessage(
        content="", tool_calls=[{"name": name, "args": {}, "id": "forged"}]
    )
    async with create_deep_agent(
        model(forged, AIMessage(content="done")), **options(effects)
    ) as owner:
        with pytest.raises(
            ConfigurationError, match="Captured tool binding has changed"
        ):
            await owner.ainvoke({"messages": [("user", "run")]})
    assert effects == []
    assert not PolicyClient.instances[-1].requests


@pytest.mark.parametrize(
    "change",
    [
        {"tool_surfaces": (ToolSurface((), ("read_file",)),)},
        {
            "tool_surfaces": (
                ToolSurface((), ("read_file", "task", "missing")),
                ToolSurface(("worker",), ("effect",)),
                ToolSurface(("reporting",), ()),
            )
        },
        {
            "tool_surfaces": (
                ToolSurface((), ("write_file", "task")),
                ToolSurface(("worker",), ("effect",)),
                ToolSurface(("reporting",), ()),
            )
        },
        {"catalog_descriptions": (CatalogDescription(("missing",), "effect", "Test"),)},
        {
            "catalog_descriptions": (
                CatalogDescription(("worker",), "effect", "bad\ntext"),
            )
        },
        {
            "catalog_descriptions": (
                CatalogDescription(("worker",), "effect", "x" * 241),
            )
        },
        {
            "catalog_descriptions": (CatalogDescription(("worker",), "effect", "Test"),)
            * 2
        },
        {"catalog_naming": "unknown"},
        {"include_general_purpose": 1},
    ],
)
async def test_invalid_configuration_fails_before_registration(change):
    with pytest.raises(ConfigurationError):
        async with create_deep_agent(
            model(AIMessage(content="done")), **options([], **change)
        ):
            pytest.fail("invalid surface entered")
    assert not PolicyClient.instances


async def test_legacy_names_unchanged():
    async with create_deep_agent(
        model(AIMessage(content="done")), tools=[effect([])], openeight=config()
    ) as owner:
        assert mapped_name((), "effect") in {
            f.name for f in owner.catalog_snapshot().facts
        }


async def test_unqualified_child_name_rejected():
    opts = options([])
    opts["subagents"][0]["name"] = "invalid.name"
    opts["tool_surfaces"] = tuple(
        replace(s, child_path=("invalid.name",)) if s.child_path == ("worker",) else s
        for s in opts["tool_surfaces"]
    )
    with pytest.raises(ConfigurationError):
        async with create_deep_agent(model(AIMessage(content="done")), **opts):
            pytest.fail("ambiguous qualified name entered")
    assert not PolicyClient.instances


async def test_selected_composition_preserves_harness_instructions(monkeypatch):
    import deepagents.graph as native
    from deepagents.profiles.harness import harness_profiles
    from deepagents.profiles.harness.harness_profiles import HarnessProfile
    from pydantic import Field
    from test_framework_boundary import ToolModel

    class RecordingModel(ToolModel):
        seen: list = Field(default_factory=list)

        async def _agenerate(self, messages, **kwargs):
            self.seen.extend(messages)
            return await super()._agenerate(messages, **kwargs)

    profile = HarnessProfile(
        system_prompt_suffix="Required native instructions",
        tool_description_overrides={
            "effect": "Native effect instructions.",
            "read_file": "Native file instructions.",
            "task": "Native delegation: {available_agents}",
        },
    )
    monkeypatch.setattr(native, "_harness_profile_for_model", lambda *args: profile)
    monkeypatch.setattr(
        harness_profiles, "_harness_profile_for_model", lambda *args: profile
    )
    worker = RecordingModel(responses=[AIMessage(content="done")])
    opts = options([])
    opts["subagents"][0]["model"] = worker
    opts["subagents"][0]["system_prompt"] = "Worker instructions"
    opts["catalog_descriptions"] = (
        CatalogDescription(("worker",), "effect", "Operator summary."),
    )
    async with create_deep_agent(
        model(call("task", description="run", subagent_type="worker")), **opts
    ) as owner:
        graph = owner._session._owner._graph
        tools = graph.nodes["tools"].bound.tools_by_name
        assert tools["read_file"].description == "Native file instructions."
        assert tools["task"].description.startswith("Native delegation:")
        children = getclosurevars(tools["task"].coroutine).nonlocals["subagent_graphs"]
        assert (
            children["worker"].nodes["tools"].bound.tools_by_name["effect"].description
            == "Native effect instructions."
        )
        await owner.ainvoke({"messages": [("user", "run")]})
    assert any(
        "Required native instructions" in str(m.content)
        and "Worker instructions" in str(m.content)
        for m in worker.seen
    )


@pytest.mark.parametrize("enabled", [True, False])
async def test_explicit_general_purpose_overrides_native_enablement(
    monkeypatch, enabled
):
    import deepagents.graph as native
    from deepagents.profiles.harness import harness_profiles
    from deepagents.profiles.harness.harness_profiles import (
        GeneralPurposeSubagentProfile,
        HarnessProfile,
    )

    profile = HarnessProfile(
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=not enabled)
    )
    monkeypatch.setattr(native, "_harness_profile_for_model", lambda *args: profile)
    monkeypatch.setattr(
        harness_profiles, "_harness_profile_for_model", lambda *args: profile
    )
    opts = options([], tool_surfaces=None, include_general_purpose=enabled)
    async with create_deep_agent(model(), **opts) as owner:
        tools = owner._session._owner._graph.nodes["tools"].bound.tools_by_name
        children = getclosurevars(tools["task"].coroutine).nonlocals["subagent_graphs"]
        assert ("general-purpose" in children) is enabled


async def test_explicit_construction_without_children_preserves_root_only_topology():
    async with create_deep_agent(
        model(), openeight=config(), include_general_purpose=False
    ) as owner:
        assert (
            "task"
            not in owner._session._owner._graph.nodes["tools"].bound.tools_by_name
        )
        result = await owner.ainvoke({"messages": [("user", "hello")]})
        assert result["messages"][-1].content == "done"


@pytest.mark.parametrize("role", ["worker", "reporting"])
@pytest.mark.parametrize(
    "name",
    [
        "read_file",
        "write_file",
        "edit_file",
        "delete",
        "glob",
        "grep",
        "execute",
        "task",
    ],
)
async def test_excluded_child_calls_cannot_reach_an_executor(role, name):
    effects = []
    forged = AIMessage(
        content="", tool_calls=[{"name": name, "args": {}, "id": "forged-child"}]
    )
    opts = options(effects)
    for child in opts["subagents"]:
        if child["name"] == role:
            child["model"] = model(forged)
    async with create_deep_agent(
        model(call("task", description="run", subagent_type=role)), **opts
    ) as owner:
        with pytest.raises(
            ConfigurationError, match="Captured tool binding has changed"
        ):
            await owner.ainvoke({"messages": [("user", "run")]})
    assert effects == []
    assert len(PolicyClient.instances[-1].requests) == 1  # Delegation only.


@pytest.mark.parametrize("name", ["coordinator", "bad.name", "UPPER"])
async def test_toolless_child_names_are_validated(name):
    opts = options([])
    opts["subagents"][1]["name"] = name
    opts["tool_surfaces"] = tuple(
        replace(s, child_path=(name,)) if s.child_path == ("reporting",) else s
        for s in opts["tool_surfaces"]
    )
    with pytest.raises(ConfigurationError):
        async with create_deep_agent(model(), **opts):
            pytest.fail("invalid tool-less child entered")
    assert not PolicyClient.instances


async def test_shared_remote_identity_retains_both_dispatch_paths():
    from types import SimpleNamespace

    from openeight.langchain._bindings import AdapterBindings

    tool = effect([])
    tool.name = "mcp_remote_identity"
    remote = SimpleNamespace(tool=tool)
    bindings = AdapterBindings(
        {(): [tool], ("child",): [tool]}, deep=True, remotes={id(tool): remote}
    )
    assert len(bindings.facts) == 1
    assert bindings.resolve((), tool.name).remote is remote
    assert bindings.resolve(("child",), tool.name).remote is remote
    with pytest.raises(ConfigurationError):
        AdapterBindings(
            {(): [tool], ("child",): [tool]},
            deep=True,
            remotes={id(tool): remote},
            descriptions={(("child",), tool.name): "Conflicting remote description."},
        )


async def test_qualified_length_applies_to_actual_name_not_synthetic_suffix():
    from openeight.deepagents._surface import qualified_name, validate_qualified_path

    path = ("a" * 250,)
    validate_qualified_path(path)
    assert qualified_name(path, "x") == "a" * 250 + ".x"
    with pytest.raises(ConfigurationError):
        qualified_name(path, "longname")
