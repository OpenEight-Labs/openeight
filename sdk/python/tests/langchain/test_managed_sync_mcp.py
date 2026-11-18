"""Public synchronous MCP operations use the same live discovery and dispatch gate."""

import json

import pytest

pytest.importorskip("mcp")

from _policy_helpers import PolicyClient, call
from langchain_core.messages import AIMessage
from test_managed_mcp import make_source, options
from test_managed_sync_boundaries import HistoryModel

from openeight import ConfigurationError
from openeight.langchain import create_agent
from openeight.mcp._source import tool_name

INPUT = {"messages": [("user", "go")]}


@pytest.mark.parametrize("disposition", ["allow", "deny", "unconfigured"])
def test_sync_mcp_invoke_batch_refresh_and_lazy_stream_keep_one_owner(
    monkeypatch, tmp_path, disposition
):
    remote, state, receipts = make_source(tmp_path, monkeypatch)
    name = tool_name(remote, "effect")
    model = HistoryModel(responses=[call(name), AIMessage(content="done")])
    owner = create_agent(model, openeight=options(remote))
    with owner:
        client = PolicyClient.instances[0]
        if disposition == "deny":
            client.reject_name = name
        if disposition == "unconfigured":
            client.pending = True
        assert owner.flush().pending_records == 0
        assert owner.invoke(INPUT)["messages"][-1].content == "done"
        results = owner.batch([INPUT, INPUT], {"max_concurrency": 2})
        assert [result["messages"][-1].content for result in results] == [
            "done",
            "done",
        ]
        state.write_text(json.dumps({"tools": ["effect", "second"]}))
        try:
            owner.refresh_tools()
        except ConfigurationError as error:
            assert error.reason == "tool_catalog_stale"
            owner.refresh_tools()
        assert len(owner.catalog_snapshot().facts) == 2
        stream = owner.stream(INPUT, stream_mode="updates")
        before = len(client.requests)
        assert not owner._session._bridge.busy.locked()
        with stream:
            assert list(stream)
        assert len(client.requests) == before + (
            0 if disposition == "unconfigured" else 1
        )
        assert owner._session._bridge.thread.is_alive()
    effects = (
        [json.loads(line) for line in receipts.read_text().splitlines()]
        if receipts.exists()
        else []
    )
    assert len(effects) == (4 if disposition == "allow" else 0)
    assert all(effect["arguments"] == {"value": "original"} for effect in effects)
    assert not owner._session._bridge.thread.is_alive()
