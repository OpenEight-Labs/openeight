"""Governed native reads retain path-derived identity without a Decision."""

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from langchain_core.tools import tool
from test_managed_deepagents import PolicyClient, call, config, model

from openeight import ToolReadiness
from openeight.deepagents import create_deep_agent
from openeight.langchain import _owner
from openeight.langchain._bindings import mapped_name


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [(), ("general-purpose",), ("researcher",)])
async def test_governed_read_uses_captured_path_identity(monkeypatch, path):
    name = mapped_name(path, "lookup")
    revision = str(uuid4())

    class ReadClient(PolicyClient):
        def register_tools(self, facts, **kwargs):
            results = super().register_tools(facts, **kwargs)
            changed = []
            for result in results:
                if result.tool.name == name:
                    result = replace(
                        result,
                        tool=replace(result.tool, capability="read"),
                        tool_version=replace(
                            result.tool_version,
                            tool_contract_digest=None,
                            governance_revision_id=revision,
                        ),
                        readiness=ToolReadiness(
                            result.request_index,
                            name,
                            result.tool.id,
                            result.tool_version.id,
                            "registered",
                            True,
                            True,
                            True,
                            "read_only",
                        ),
                    )
                if result.tool.name == name:
                    self.current_read = result
                changed.append(result)
                self.publications[result.tool_version.id] = result
            return tuple(changed)

    PolicyClient.instances = []
    monkeypatch.setattr(_owner, "Client", ReadClient)
    reads = []

    @tool
    async def lookup(value: str) -> str:
        """Read a synthetic value."""
        reads.append(value)
        return value

    child = (
        dict(
            name=path[0],
            description="lookup",
            model=model(call("lookup")),
            tools=[lookup],
        )
        if path
        else None
    )
    root = (
        model(call("task", description="lookup", subagent_type=path[0]))
        if path
        else model(call("lookup"))
    )
    async with create_deep_agent(
        root, tools=[lookup], subagents=[child] if child else [], openeight=config()
    ) as owner:
        await owner.ainvoke({"messages": [("user", "lookup")]})
        await owner.aflush()
        client = PolicyClient.instances[-1]
        current = client.current_read
        events = [
            e
            for write in client.writes
            for e in write.get("events", [])
            if e.get("tool_id") == current.tool.id
        ]
        assert len(events) == 1
        assert events[0]["tool_name"] == name
        assert events[0]["tool_version_id"] == current.tool_version.id
        assert "decision_id" not in events[0]
        assert (
            json.loads(events[0]["attributes_json"])[
                "openeight.tool.governance_revision_id"
            ]
            == revision
        )
        assert not [r for r in client.requests if r["tool_id"] == current.tool.id]
    assert reads == ["original"]
