"""Compiled native mutation must fail before a catalog or handler is reachable."""

import pytest

pytest.importorskip("deepagents")
pytest.importorskip("aiosqlite")

import langchain.agents
from test_deep_durable_engine import NativeClient, candidate_selection
from test_local_durable_facade import candidate

from openeight import ConfigurationError
from openeight.deepagents._middleware import GovernanceBoundary
from openeight.langchain import _owner
from openeight.langchain._frameworks import v1 as _langchain_v1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["remove", "move", "root_dispatch", "child_dispatch"]
)
async def test_native_governance_gate_mutation_fails_before_publication(
    tmp_path, monkeypatch, mutation
):
    candidate_selection(monkeypatch)
    client = NativeClient()
    monkeypatch.setattr(_owner, "Client", lambda *a, **kw: client)
    native = langchain.agents.create_agent
    assert _langchain_v1.native_create is native

    def mutated(*args, **options):
        stack = list(options["middleware"])
        gate = next(item for item in stack if type(item) is GovernanceBoundary)
        if mutation in {"remove", "move"}:
            stack.remove(gate)
            if mutation == "move":
                stack.insert(0, gate)
            options["middleware"] = stack
        graph = native(*args, **options)
        is_root = "task" in graph.nodes["tools"].bound.tools_by_name
        if mutation == "root_dispatch" and is_root:
            graph.nodes["tools"].bound._awrap_tool_call = None
        if mutation == "child_dispatch" and is_root:
            from inspect import getclosurevars

            task = graph.nodes["tools"].bound.tools_by_name["task"]
            children = getclosurevars(task.coroutine).nonlocals["subagent_graphs"]
            children["worker"].nodes["tools"].bound._awrap_tool_call = None
        return graph

    monkeypatch.setattr(langchain.agents, "create_agent", mutated)
    with pytest.raises(ConfigurationError) as caught:
        async with candidate(tmp_path):
            pytest.fail("mutated native graph admitted")
    assert caught.value.reason == "unsupported_graph_shape"
    assert client.registrations == [] and client.requests == []
