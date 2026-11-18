"""Static raw middleware must agree with its native compiled task executors."""

from inspect import getclosurevars

import pytest

pytest.importorskip("deepagents")

from deepagents.backends import StateBackend
from deepagents.middleware.subagents import SubAgentMiddleware
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from test_framework_boundary import ToolModel

from openeight import ConfigurationError
from openeight.deepagents._frameworks import _nested


@tool
def effect(value: str) -> str:
    """Return the original callable's value."""
    return value


def declaration(*, middleware=(), prompt="Delegate to the listed native child."):
    spec = dict(
        name="leaf",
        description="Perform the native leaf work.",
        system_prompt="Leaf instructions.",
        model=ToolModel(responses=[AIMessage(content="done")]),
        tools=[effect],
        middleware=list(middleware),
    )
    return SubAgentMiddleware(
        backend=StateBackend(), subagents=[spec], system_prompt=prompt
    )


@pytest.mark.parametrize("with_todos", [False, True])
def test_native_baseline_and_copy_preserve_complete_prompt_without_double_expansion(
    with_todos,
):
    original = declaration(middleware=[TodoListMiddleware()] if with_todos else [])
    specs = _nested.validate(original)
    copied = _nested.reconstruct(original, specs, backend=StateBackend())
    assert copied is not original
    assert copied.system_prompt == original.system_prompt
    assert copied.tools[0].description == original.tools[0].description
    assert copied.system_prompt.count("Available subagent types:") == 1
    assert specs[0]["model"] is original._subagents[0]["model"]


@pytest.mark.parametrize(
    "field", ["name", "description", "system_prompt", "model", "tools", "middleware"]
)
def test_post_construction_raw_mutation_cannot_replace_the_compiled_native_meaning(
    field,
):
    original = declaration()
    changed = {
        "name": "different",
        "description": "A changed description.",
        "system_prompt": "Changed instructions.",
        "model": ToolModel(responses=[AIMessage(content="different")]),
        "tools": [],
        "middleware": [TodoListMiddleware()],
    }
    original._subagents[0][field] = changed[field]
    with pytest.raises(ConfigurationError) as caught:
        _nested.validate(original)
    assert caught.value.reason == "unsupported_graph_shape"


def test_native_child_order_is_measured_even_with_no_constructor_prompt_or_description():
    first = declaration(prompt=None)._subagents[0]
    second = {**first, "name": "other", "description": "Other native child."}
    original = SubAgentMiddleware(backend=StateBackend(), subagents=[first, second])
    original._subagents.reverse()
    with pytest.raises(ConfigurationError):
        _nested.validate(original)


@pytest.mark.parametrize(
    "field,value",
    [
        ("_private_state_keys", frozenset({"files"})),
        ("_state_schema", dict),
        ("_task_description", "Changed after compilation."),
        ("subagent_names", frozenset({"foreign"})),
    ],
)
def test_unqualified_native_options_or_mutated_indexes_fail_before_reconstruction(
    field, value
):
    original = declaration()
    setattr(original, field, value)
    with pytest.raises(ConfigurationError):
        _nested.validate(original)


def test_custom_backend_and_cyclic_declaration_are_not_static_native_state():
    original = declaration()
    original._backend.instance_override = True
    with pytest.raises(ConfigurationError):
        _nested.validate(original)
    original = declaration()
    original._subagents[0]["middleware"].append(original)
    with pytest.raises(ConfigurationError):
        _nested.validate(original)


@pytest.mark.parametrize(
    "surface",
    [
        "task_callable",
        "compiled_prompt",
        "compiled_tools",
        "model_executor",
        "node_mapper",
        "tool_errors",
        "channel_reducer",
    ],
)
def test_mutated_executable_closure_cannot_hide_behind_unchanged_raw_declarations(
    surface,
):
    original = declaration()
    graph = _nested._graphs(original)["leaf"]
    if surface == "task_callable":
        original.tools[0].coroutine = lambda *args, **kwargs: "bypass"
    elif surface == "compiled_prompt":
        model = graph.nodes["model"].bound
        getclosurevars(model.func).nonlocals[
            "system_message"
        ].content = "Changed executor prompt."
    elif surface == "compiled_tools":
        graph.nodes["tools"].bound.tools_by_name.clear()
    elif surface == "model_executor":
        closure = graph.nodes["model"].bound.afunc.__closure__
        names = graph.nodes["model"].bound.afunc.__code__.co_freevars
        closure[names.index("_execute_model_async")].cell_contents = (
            lambda *a, **kw: None
        )
    elif surface == "node_mapper":
        graph.nodes["model"].mapper = lambda value: value
    elif surface == "tool_errors":
        graph.nodes["tools"].bound._handle_tool_errors = False
    else:
        graph.channels["messages"].operator = lambda left, right: right
    with pytest.raises(ConfigurationError):
        _nested.validate(original)


def test_todo_copy_rejects_a_schema_or_executable_that_differs_from_native_constructor():
    original = TodoListMiddleware()
    copied = _nested.todo_copy(original)
    assert copied is not original and copied.system_prompt == original.system_prompt
    assert copied.tools[0] is not original.tools[0]
    original.tools[
        0
    ].description = "A tool-only mutation without constructor agreement."
    with pytest.raises(ConfigurationError):
        _nested.todo_copy(original)


def test_task_helper_cannot_replace_isolated_declaration_with_fork_mode():
    original = declaration()
    helper = getclosurevars(original.tools[0].func).nonlocals[
        "_validate_and_prepare_state"
    ]
    registry = getclosurevars(helper).nonlocals["subagents_by_name"]
    registry["leaf"] = {**registry["leaf"], "mode": "fork"}
    with pytest.raises(ConfigurationError):
        _nested.validate(original)
