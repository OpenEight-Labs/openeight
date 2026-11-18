"""Recognize exact qualified generated closures, never callable metadata."""

from inspect import getclosurevars
from types import CodeType

from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.subagents import _build_task_tool
from langchain.agents.middleware.todo import _awrite_todos, _write_todos


def _nested(function, name):
    return next(
        value
        for value in function.__code__.co_consts
        if isinstance(value, CodeType) and value.co_name == name
    )


def _code(function, factory, name):
    return getattr(function, "__code__", None) is _nested(factory, name)


def generated_kind(binding, *, backend, graphs):
    tool = binding.tool
    sync, asynchronous = getattr(tool, "func", None), getattr(tool, "coroutine", None)
    if binding.native_name == "write_todos":
        return (
            "state_write"
            if sync is _write_todos and asynchronous is _awrite_todos
            else None
        )
    if binding.native_name == "task":
        if not (
            _code(sync, _build_task_tool, "task")
            and _code(asynchronous, _build_task_tool, "atask")
        ):
            return None
        try:
            first = getclosurevars(sync).nonlocals
            second = getclosurevars(asynchronous).nonlocals
            children = first["subagent_graphs"]
            helper = first["_validate_and_prepare_state"]
            path = binding.child_path
            immediate = {
                p[-1]: graph
                for p, graph in graphs.items()
                if len(p) == len(path) + 1 and p[:-1] == path
            }
            if (
                second["subagent_graphs"] is not children
                or second["_validate_and_prepare_state"] is not helper
                or not _code(helper, _build_task_tool, "_validate_and_prepare_state")
                or set(children) != set(immediate)
                or any(children[name] is not graph for name, graph in immediate.items())
            ):
                return None
        except (KeyError, TypeError):
            return None
        return "delegation"
    names = {
        "write_file": (
            "_create_write_file_tool",
            "sync_write_file",
            "async_write_file",
        ),
        "edit_file": ("_create_edit_file_tool", "sync_edit_file", "async_edit_file"),
        "delete": ("_create_delete_tool", "sync_delete", "async_delete"),
    }
    expected = names.get(binding.native_name)
    if expected is None or backend is None:
        return None
    factory, sync_name, async_name = expected
    factory = getattr(FilesystemMiddleware, factory)
    if not (
        _code(sync, factory, sync_name) and _code(asynchronous, factory, async_name)
    ):
        return None
    try:
        owner = getclosurevars(sync).nonlocals["self"]
        if (
            type(owner) is not FilesystemMiddleware
            or getclosurevars(asynchronous).nonlocals["self"] is not owner
            or owner.backend is not backend
        ):
            return None
    except (KeyError, TypeError):
        return None
    return "state_write"
