"""Measure captured constructor facts without serializing models or callables."""

import hashlib

from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.todo import _awrite_todos, _write_todos

from openeight.errors import ConfigurationError

from .._deployment import canonical


def _hash(value):
    if value is None:
        return None
    from langchain_core.messages import SystemMessage

    from openeight._framework._measurement import payload_hash

    if type(value) is SystemMessage:
        return payload_hash(value.model_dump(mode="json"))
    if type(value) is not str:
        raise ConfigurationError("unsupported_graph_shape")
    try:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    except UnicodeError:
        raise ConfigurationError("unsupported_graph_shape") from None


def _middleware(items):
    from deepagents.middleware.subagents import SubAgentMiddleware

    from . import _nested

    result = []
    for item in items:
        if type(item) is SubAgentMiddleware:
            result.append(
                {
                    "kind": type(item).__module__ + "." + type(item).__qualname__,
                    "options": {
                        "system_prompt_sha256": _hash(item.system_prompt),
                        "task_description_sha256": _hash(item._task_description),
                        "effective_task_description_sha256": _hash(
                            item.tools[0].description
                        ),
                        "children": [
                            {
                                "name": child["name"],
                                "description_sha256": _hash(child["description"]),
                                "prompt_sha256": _hash(child.get("system_prompt")),
                                "middleware": _middleware(child.get("middleware", ())),
                            }
                            for child in _nested.children(item)
                        ],
                    },
                }
            )
            continue
        if (
            type(item) is not TodoListMiddleware
            or len(item.tools) != 1
            or item.tools[0].func is not _write_todos
            or item.tools[0].coroutine is not _awrite_todos
        ):
            raise ConfigurationError("unsupported_graph_shape")
        result.append(
            {
                "kind": type(item).__module__ + "." + type(item).__qualname__,
                "options": {
                    "system_prompt_sha256": _hash(item.system_prompt),
                    "tool_description_sha256": _hash(item.tools[0].description),
                },
            }
        )
    return result


def local_configuration_descriptor(*, constructor_options, composition):
    if constructor_options["skills"] or constructor_options["memory"]:
        raise ConfigurationError("unsupported_graph_shape")
    children = []
    for child in constructor_options["children"]:
        if child.get("skills"):
            raise ConfigurationError("unsupported_graph_shape")
        children.append(
            {
                "path": [child["name"]],
                "description_sha256": _hash(child["description"]),
                "prompt_sha256": _hash(child.get("system_prompt")),
                "middleware": _middleware(child.get("middleware", ())),
            }
        )
    descriptor = {
        "format_version": 1,
        "root_prompt_sha256": _hash(constructor_options["system_prompt"]),
        "children": sorted(children, key=lambda c: canonical(c["path"]).encode()),
        "tool_surfaces": [
            {
                "path": node["path"],
                "native_names": [t["native_name"] for t in node["tools"]],
            }
            for node in composition["topology"]
        ],
        "catalog_naming": composition["catalog_naming"],
        "backend": composition["backend"],
        "saver": composition["saver"],
        "skills": [],
        "memory": [],
        "middleware": _middleware(constructor_options["middleware"]),
    }
    return canonical(descriptor)
