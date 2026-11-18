"""Qualified harness and static child-closure inspection."""

from inspect import getclosurevars

from langchain_core.language_models import BaseChatModel

from openeight import ConfigurationError
from openeight.langchain._compat import inspect_agent


def profile_for(model, *, resolve_profile, original_model_string=None):
    if not isinstance(model, BaseChatModel):
        raise ConfigurationError("invalid_integration_configuration")
    failed = False
    profile = None
    try:
        profile = resolve_profile(model, original_model_string)
    except Exception:
        failed = True
    if failed:
        raise ConfigurationError("invalid_integration_configuration")
    # Tail middleware and exclusions can alter dispatch after the SDK boundary.
    if (
        profile.extra_middleware
        or profile.excluded_middleware
        or profile.excluded_tools
    ):
        raise ConfigurationError("unsupported_graph_shape")
    gp = profile.general_purpose_subagent
    if (
        gp is not None
        and gp.system_prompt is not None
        and profile.base_system_prompt is not None
    ):
        raise ConfigurationError("unsupported_graph_shape")
    return profile


def native_graphs(graph, boundaries):
    graphs = {}

    def visit(graph, path):
        graphs[path] = graph
        task = (
            graph.nodes["tools"].bound.tools_by_name.get("task")
            if "tools" in graph.nodes
            else None
        )
        expected = {
            p[-1] for p in boundaries if len(p) == len(path) + 1 and p[:-1] == path
        }
        if task is None:
            if expected:
                raise ConfigurationError("unsupported_graph_shape")
            return
        if (
            getattr(task.coroutine, "__module__", None)
            != "deepagents.middleware.subagents"
        ):
            raise ConfigurationError("unsupported_graph_shape")
        try:
            sync = getclosurevars(task.func).nonlocals["subagent_graphs"]
            children = getclosurevars(task.coroutine).nonlocals["subagent_graphs"]
        except (KeyError, TypeError):
            raise ConfigurationError("unsupported_graph_shape") from None
        if children is not sync or set(children) != expected:
            raise ConfigurationError("unsupported_graph_shape")
        for name, child in children.items():
            visit(child, (*path, name))

    visit(graph, ())
    if set(graphs) != set(boundaries):
        raise ConfigurationError("unsupported_graph_shape")
    return graphs


def capture(graph, boundaries, surfaces=None):
    graphs = native_graphs(graph, boundaries)
    result = {}
    for path, child in graphs.items():
        capability = inspect_agent(child, boundaries[path])
        if capability.discovery_completeness != "complete":
            raise ConfigurationError("unsupported_graph_shape")
        if (
            surfaces is not None
            and {t.name for t in capability.tools} != surfaces[path]
        ):
            raise ConfigurationError("invalid_tool_surface")
        result[path] = capability.tools
    return result
