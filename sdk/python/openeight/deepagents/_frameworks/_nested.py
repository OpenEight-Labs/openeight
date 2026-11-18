"""Validate static native declarations against their already compiled executors."""

from collections import defaultdict
from functools import partial
from inspect import getclosurevars, isfunction, ismethod

from deepagents.backends import StateBackend
from deepagents.middleware.subagents import SubAgentMiddleware
from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.todo import _awrite_todos, _write_todos
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool
from typing_extensions import is_typeddict

from openeight.errors import ConfigurationError
from openeight.langchain._bindings import executable_identity
from openeight.langchain._catalog import tool_schema

from .._deployment import canonical

CHILD_KEYS = frozenset(
    {
        "name",
        "description",
        "system_prompt",
        "model",
        "tools",
        "middleware",
        "skills",
        "interrupt_on",
    }
)
_INSTANCE_KEYS = frozenset(
    {
        "_backend",
        "_subagents",
        "_private_state_keys",
        "_task_description",
        "_state_schema",
        "subagent_names",
        "system_prompt",
        "tools",
    }
)


def invalid():
    raise ConfigurationError("unsupported_graph_shape")


def children(item):
    if (
        type(item) is not SubAgentMiddleware
        or set(vars(item)) != _INSTANCE_KEYS
        or type(item._backend) is not StateBackend
        or vars(item._backend)
        or type(item._private_state_keys) is not frozenset
        or item._private_state_keys
        or item._state_schema is not None
        or item.system_prompt is not None
        and type(item.system_prompt) is not str
        or item._task_description is not None
        and type(item._task_description) is not str
        or type(item._subagents) not in {list, tuple}
        or not 1 <= len(item._subagents) <= 200
    ):
        invalid()
    values = tuple(item._subagents)
    names = set()
    for spec in values:
        if (
            type(spec) is not dict
            or set(spec) - CHILD_KEYS
            or not {"name", "description", "model", "tools"} <= set(spec)
            or type(spec["name"]) is not str
            or not spec["name"]
            or len(spec["name"].encode()) > 256
            or spec["name"] in names
            or type(spec["description"]) is not str
            or not isinstance(spec["model"], BaseChatModel)
            or spec.get("system_prompt") is not None
            and type(spec["system_prompt"]) is not str
            or spec.get("skills")
            or spec.get("interrupt_on") is not None
            or type(spec["tools"]) not in {list, tuple}
            or type(spec.get("middleware", ())) not in {list, tuple}
        ):
            invalid()
        names.add(spec["name"])
    if item.subagent_names != frozenset(names):
        invalid()
    return values


def todo_copy(item):
    if (
        type(item) is not TodoListMiddleware
        or set(vars(item)) != {"system_prompt", "tool_description", "tools"}
        or type(item.system_prompt) is not str
        or len(item.tools) != 1
        or item.tools[0].func is not _write_todos
        or item.tools[0].coroutine is not _awrite_todos
        or type(item.tools[0].description) is not str
        or item.tool_description != item.tools[0].description
    ):
        invalid()
    copied = TodoListMiddleware(
        system_prompt=item.system_prompt, tool_description=item.tools[0].description
    )
    if _tool_signature(copied.tools[0]) != _tool_signature(item.tools[0]):
        invalid()
    return copied


def _tool_signature(tool):
    return (
        tool.name,
        tool.description,
        canonical(tool_schema(tool)),
        executable_identity(tool),
    )


def _graphs(item):
    try:
        if type(item.tools) is not list or len(item.tools) != 1:
            invalid()
        task = item.tools[0]
        executable_identity(task)
        if task.name != "task":
            invalid()
        sync = getclosurevars(task.func).nonlocals["subagent_graphs"]
        asynchronous = getclosurevars(task.coroutine).nonlocals["subagent_graphs"]
        if type(sync) is not dict or sync is not asynchronous:
            invalid()
        return sync
    except (KeyError, TypeError, AttributeError):
        invalid()


def _schema_signature(value):
    if is_typeddict(value):
        return (
            "typed-dict",
            value.__module__,
            value.__qualname__,
            tuple(sorted(value.__annotations__.items())),
            value.__required_keys__,
            value.__optional_keys__,
        )
    return value


def _native_object(value):
    return type(value).__module__.startswith(
        (
            "langgraph.",
            "langchain.",
            "langchain_core.",
            "deepagents.",
        )
    )


def _value_signature(value, allowed, seen):
    if isfunction(value) or ismethod(value):
        return _callable_signature(value, allowed, seen)
    if is_typeddict(value):
        return _schema_signature(value)
    if isinstance(value, BaseChatModel):
        return "borrowed-model", type(value), id(value)
    if isinstance(value, BaseTool):
        return "tool", _tool_signature(value)
    if (
        value is None
        or isinstance(value, type)
        or type(value) in {str, int, bool, float, bytes}
    ):
        return value
    from typing import get_args, get_origin

    origin = get_origin(value)
    if origin is not None:
        return "annotation", origin, get_args(value)
    if id(value) in seen:
        return "recursive-value", type(value)
    seen.add(id(value))
    try:
        if isinstance(value, partial):
            return (
                "partial",
                _value_signature(value.func, allowed, seen),
                _value_signature(value.args, allowed, seen),
                _value_signature(value.keywords, allowed, seen),
            )
        if isinstance(value, (tuple, list)):
            return "sequence", tuple(_value_signature(v, allowed, seen) for v in value)
        if type(value) in {dict, defaultdict}:
            pairs = [
                (_value_signature(k, allowed, seen), _value_signature(v, allowed, seen))
                for k, v in value.items()
            ]
            return (
                "mapping",
                _value_signature(value.default_factory, allowed, seen)
                if type(value) is defaultdict
                else None,
                tuple(sorted(pairs, key=lambda pair: repr(pair[0]))),
            )
        if type(value) in {set, frozenset}:
            return "set", tuple(
                sorted((_value_signature(v, allowed, seen) for v in value), key=repr)
            )
        if _native_object(value):
            attributes = dict(vars(value)) if hasattr(value, "__dict__") else {}
            for cls in type(value).__mro__:
                slots = vars(cls).get("__slots__", ())
                if type(slots) is str:
                    slots = (slots,)
                for key in slots:
                    if key not in {"__dict__", "__weakref__"} and hasattr(value, key):
                        attributes[key] = getattr(value, key)
            return type(value), _value_signature(attributes, allowed, seen)
        return "borrowed", type(value), id(value)
    finally:
        seen.remove(id(value))


def _callable_signature(function, allowed, seen=None):
    if function is None:
        return None
    seen = set() if seen is None else seen
    if ismethod(function):
        instance = function.__self__
        if any(instance is item for item in allowed):
            owner = (type(instance), id(instance))
        elif _native_object(instance):
            owner = _value_signature(instance, allowed, seen)
        else:
            invalid()
        return function.__func__, owner
    if not isfunction(function):
        invalid()
    if id(function) in seen:
        return "recursive", function.__code__, id(function.__globals__)
    seen.add(id(function))
    try:
        values = []
        for key, value in sorted(getclosurevars(function).nonlocals.items()):
            if key == "subagent_graphs":
                # Each named executor and declaration is validated separately.
                values.append((key, "static_registry"))
            else:
                values.append((key, _value_signature(value, allowed, seen)))
        return function.__code__, id(function.__globals__), tuple(values)
    finally:
        seen.remove(id(function))


def _graph_signature(graph, middleware):
    try:
        from langgraph.graph.state import CompiledStateGraph

        if type(graph) is not CompiledStateGraph:
            invalid()
        model_node = graph.builder.nodes["model"].runnable
        if model_node is not graph.nodes["model"].bound:
            invalid()
        sync = getclosurevars(model_node.func).nonlocals
        asynchronous = getclosurevars(model_node.afunc).nonlocals
        if (
            model_node.func.__module__ != "langchain.agents.factory"
            or model_node.func.__qualname__ != "create_agent.<locals>.model_node"
            or model_node.afunc.__qualname__ != "create_agent.<locals>.amodel_node"
            or sync["model"] is not asynchronous["model"]
            or sync["system_message"] != asynchronous["system_message"]
            or sync["initial_response_format"] is not None
            or sync["system_message"] is not None
            and type(sync["system_message"]) is not SystemMessage
        ):
            invalid()
        tools = tuple(_tool_signature(t) for t in sync["default_tools"])
        if tools != tuple(_tool_signature(t) for t in asynchronous["default_tools"]):
            invalid()
        hooks = []
        for name, node in sorted(graph.builder.nodes.items()):
            if name in {"model", "tools"}:
                continue
            runnable = node.runnable
            hooks.append(
                (
                    name,
                    _callable_signature(runnable.func, middleware),
                    _callable_signature(runnable.afunc, middleware),
                )
            )
        tool_node = graph.nodes.get("tools")
        if tool_node is None:
            dispatch = None
            if tools:
                invalid()
        else:
            bound = tool_node.bound
            if bound is not graph.builder.nodes["tools"].runnable:
                invalid()
            if {t[0] for t in tools} != set(bound.tools_by_name):
                invalid()
            dispatch = (
                tuple(
                    sorted(
                        (_tool_signature(t) for t in bound.tools_by_name.values()),
                        key=lambda v: v[0],
                    )
                ),
                _callable_signature(bound._wrap_tool_call, middleware),
                _callable_signature(bound._awrap_tool_call, middleware),
            )
        edges = tuple(
            sorted(
                (e.source, e.target, str(e.data), e.conditional)
                for e in graph.get_graph().edges
            )
        )

        def channel_type(value):
            if is_typeddict(value):
                return (
                    value.__module__,
                    value.__qualname__,
                    tuple(sorted(value.__annotations__.items())),
                    value.__required_keys__,
                    value.__optional_keys__,
                )
            return value

        channels = tuple(
            sorted(
                (
                    name,
                    type(channel),
                    channel_type(channel.ValueType),
                    channel_type(channel.UpdateType),
                )
                for name, channel in graph.channels.items()
            )
        )
        return (
            graph.name,
            id(sync["model"]),
            sync["system_message"],
            tools,
            dispatch,
            _callable_signature(sync["wrap_model_call_handler"], middleware),
            _callable_signature(asynchronous["awrap_model_call_handler"], middleware),
            tuple(hooks),
            edges,
            channels,
            _value_signature(graph, middleware, set()),
        )
    except (KeyError, TypeError, AttributeError, ValueError):
        invalid()


def validate(item, *, ancestors=()):
    if id(item) in ancestors or len(ancestors) >= 16:
        invalid()
    values = children(item)
    for spec in values:
        for child in spec.get("middleware", ()):
            if type(child) is TodoListMiddleware:
                todo_copy(child)
            elif type(child) is SubAgentMiddleware:
                validate(child, ancestors=(*ancestors, id(item)))
            else:
                invalid()
    baseline = SubAgentMiddleware(
        backend=StateBackend(),
        subagents=values,
        system_prompt=None,
        task_description=item._task_description,
    )
    baseline.system_prompt = item.system_prompt
    original_graphs, current_graphs = _graphs(item), _graphs(baseline)
    if set(original_graphs) != set(current_graphs):
        invalid()
    for actual, expected in zip(item.tools, baseline.tools):
        if (
            actual.description != expected.description
            or canonical(tool_schema(actual)) != canonical(tool_schema(expected))
            or _callable_signature(actual.func, ())
            != _callable_signature(expected.func, ())
            or _callable_signature(actual.coroutine, ())
            != _callable_signature(expected.coroutine, ())
        ):
            invalid()
    for spec in values:
        middleware = tuple(spec.get("middleware", ()))
        if _graph_signature(
            original_graphs[spec["name"]], middleware
        ) != _graph_signature(current_graphs[spec["name"]], middleware):
            invalid()
    return values


def reconstruct(item, specs, *, backend):
    copied = SubAgentMiddleware(
        backend=backend,
        subagents=specs,
        system_prompt=None,
        task_description=item._task_description,
    )
    copied.system_prompt = item.system_prompt
    return copied
