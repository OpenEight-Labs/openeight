"""Qualified native node and middleware layout inspection."""

from dataclasses import dataclass

from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from openeight.errors import ConfigurationError


@dataclass(frozen=True)
class AgentCapabilities:
    layout_id: str
    tools: tuple
    discovery_completeness: str
    discovery_diagnostic: str | None
    sync_middleware_binding: bool
    async_middleware_binding: bool


def inspect_agent(graph, middleware=None, *, validate):
    validate()
    if not isinstance(graph, CompiledStateGraph) or "model" not in graph.nodes:
        raise ConfigurationError("unsupported_graph_shape") from None
    model_function = getattr(graph.builder.nodes["model"].runnable, "func", None)
    if (
        getattr(model_function, "__module__", None) != "langchain.agents.factory"
        or getattr(model_function, "__qualname__", None)
        != "create_agent.<locals>.model_node"
    ):
        raise ConfigurationError("unsupported_graph_shape") from None
    if middleware is not None:
        hook = f"{middleware.name}.before_agent"
        if hook not in graph.nodes:
            raise ConfigurationError("unsupported_graph_shape") from None
        node = graph.builder.nodes[hook].runnable
        # Native RunnableCallable stores the original bound middleware hook.
        function = getattr(node, "func", None)
        if getattr(function, "__self__", None) is not middleware:
            raise ConfigurationError("unsupported_graph_shape") from None
    if "tools" not in graph.nodes:
        return AgentCapabilities(
            "native-unknown", (), "unknown", "unsupported_graph_shape", False, False
        )
    try:
        from langchain.tools.tool_node import _ToolNode as ToolNode
    except ImportError:
        from langgraph.prebuilt import ToolNode
    node = graph.nodes["tools"].bound
    if (
        not isinstance(node, ToolNode)
        or node is not graph.builder.nodes["tools"].runnable
    ):
        return AgentCapabilities(
            "native-unknown", (), "unknown", "unsupported_graph_shape", False, False
        )
    if middleware is not None:
        for name in ("_wrap_tool_call", "_awrap_tool_call"):
            function = getattr(node, name, None)
            for _ in range(200):
                closure = dict(
                    zip(
                        getattr(getattr(function, "__code__", None), "co_freevars", ()),
                        (
                            cell.cell_contents
                            for cell in getattr(function, "__closure__", None) or ()
                        ),
                    )
                )
                if (
                    getattr(function, "__module__", None) == "langchain.agents.factory"
                    and "inner" in closure
                ):
                    function = closure["inner"]
                elif (
                    "func" in closure
                    and getattr(closure["func"], "__self__", None) is middleware
                ):
                    function = closure["func"]
                    break
                else:
                    break
            if getattr(function, "__self__", None) is not middleware:
                raise ConfigurationError("unsupported_graph_shape") from None
    registry = node.tools_by_name
    if type(registry) is not dict:
        return AgentCapabilities(
            "native-unknown", (), "unknown", "unsupported_graph_shape", False, False
        )
    if len(registry) > 200:
        return AgentCapabilities(
            "native-toolnode",
            (),
            "partial",
            "tool_discovery_limit_exceeded",
            True,
            True,
        )
    tools, incomplete = [], False
    for name, tool in registry.items():
        if not isinstance(tool, BaseTool) or name != tool.name:
            incomplete = True
        else:
            tools.append(tool)
    return AgentCapabilities(
        "native-toolnode",
        tuple(tools),
        "partial" if incomplete else "complete",
        "unsupported_tool_definition" if incomplete else None,
        True,
        True,
    )
