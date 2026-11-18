"""Native framework assumptions used by the supported create_agent adapter."""

import asyncio

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from openeight import ConfigurationError


@pytest.mark.parametrize(
    ("package", "unsafe_version"),
    [
        ("langchain", "1.3.0"),
        ("langchain", "1.3.18rc1"),
        ("langgraph", "1.0.6"),
        ("langgraph-checkpoint", "3.0.1"),
        ("langgraph-sdk", "0.3.15"),
        ("langgraph-sdk", "0.4.4"),
        ("langgraph-sdk", "0.4.5"),
        ("langgraph-sdk", "0.4.6rc1"),
        ("urllib3", "2.7.0"),
    ],
)
def test_unsafe_dependency_versions_reject_before_native_graph_inspection(
    monkeypatch, package, unsafe_version
):
    from openeight.langchain import _compat

    original = _compat.version
    monkeypatch.setattr(
        _compat,
        "version",
        lambda name: unsafe_version if name == package else original(name),
    )
    with pytest.raises(ConfigurationError) as caught:
        _compat.validate_versions()
    assert caught.value.reason == "unsupported_framework_version"


class ToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class Callback(BaseCallbackHandler):
    def __init__(self):
        self.started = []

    def on_tool_start(
        self, serialized, input_str, *, run_id, parent_run_id=None, **kwargs
    ):
        self.started.append((run_id, parent_run_id))


def fixture():
    effects, order = [], []

    @tool
    def echo(value: str) -> str:
        """Echo the requested value."""
        effects.append(value)
        return value

    @tool
    def unused(value: str) -> str:
        """A bound tool that is never invoked."""
        raise AssertionError("discovery must not execute a tool")

    class TransformAndRetry(AgentMiddleware):
        def wrap_tool_call(self, request, handler):
            changed = request.override(
                tool_call={**request.tool_call, "args": {"value": "changed"}}
            )
            handler(changed)
            return handler(changed)

        async def awrap_tool_call(self, request, handler):
            changed = request.override(
                tool_call={**request.tool_call, "args": {"value": "changed"}}
            )
            await handler(changed)
            return await handler(changed)

    class Boundary(AgentMiddleware):
        def before_agent(self, state, runtime):
            order.append("binding")

        def wrap_tool_call(self, request, handler):
            order.append(request.tool_call["args"]["value"])
            return handler(request)

        async def awrap_tool_call(self, request, handler):
            order.append(request.tool_call["args"]["value"])
            return await handler(request)

    model = ToolModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "call", "name": "echo", "args": {"value": "original"}}
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    graph = create_agent(
        model, tools=[echo, unused], middleware=[TransformAndRetry(), Boundary()]
    )
    return graph, effects, order


def test_native_toolnode_identity_and_no_discovery_effects():
    graph, effects, order = fixture()
    try:
        from langchain.tools.tool_node import _ToolNode as ToolNode
    except ImportError:
        from langgraph.prebuilt import ToolNode
    node = graph.nodes["tools"].bound
    assert isinstance(node, ToolNode)
    assert node is graph.builder.nodes["tools"].runnable
    assert set(node.tools_by_name) == {"echo", "unused"}
    assert effects == [] and order == []
    assert any("Boundary.before_agent" in name for name in graph.nodes)


def test_sync_native_retry_order_and_callback_parent():
    graph, effects, order = fixture()
    callback = Callback()
    result = graph.invoke({"messages": [("user", "hello")]}, {"callbacks": [callback]})
    assert result["messages"][-1].content == "done"
    assert effects == ["changed", "changed"]
    assert order == ["binding", "changed", "changed"]
    assert len(callback.started) == 2
    assert all(parent is not None for _, parent in callback.started)
    assert len({run for run, _ in callback.started}) == 2


def test_native_sync_and_async_streams():
    for mode in ("values", "updates", "messages"):
        graph, effects, order = fixture()
        assert list(graph.stream({"messages": [("user", "hello")]}, stream_mode=mode))
        assert effects == ["changed", "changed"]

        async def exercise():
            graph, effects, order = fixture()
            chunks = [
                chunk
                async for chunk in graph.astream(
                    {"messages": [("user", "hello")]}, stream_mode=mode
                )
            ]
            assert chunks
            assert effects == ["changed", "changed"]
            assert order == ["binding", "changed", "changed"]

        asyncio.run(exercise())
