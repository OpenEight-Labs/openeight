"""Measure ordinary graph contracts while binding application code to its revision."""

import hashlib
from inspect import getclosurevars, isfunction
from typing import Any, ForwardRef, get_args, get_origin

from openeight._framework._measurement import freeze, payload_hash, qualified
from openeight.errors import ConfigurationError


def callable_contract(value):
    if isfunction(value):
        return {
            "callable": value.__module__ + "." + value.__qualname__,
            "revision_bound": True,
        }
    return {"callable": qualified(value), "revision_bound": True}


def type_contract(value):
    import typing

    for name in (
        "Annotated",
        "Union",
        "Literal",
        "Required",
        "NotRequired",
        "ClassVar",
        "Final",
    ):
        if value is getattr(typing, name):
            return "typing." + name
    if value is Any:
        return "typing.Any"
    if value is None or type(value) in (str, int, bool, float):
        return value
    if isinstance(value, ForwardRef):
        return {"forward": value.__forward_arg__}
    origin = get_origin(value)
    if origin is not None:
        return {
            "origin": type_contract(origin),
            "arguments": [type_contract(item) for item in get_args(value)],
        }
    if isinstance(value, type):
        return {"class": qualified(value)}
    if callable(value):
        return callable_contract(value)
    return {"annotation": qualified(value), "revision_bound": True}


def state_contract(schema):
    return {
        "kind": qualified(schema),
        "fields": {
            name: type_contract(annotation)
            for name, annotation in sorted(schema.__annotations__.items())
        },
        "required": sorted(getattr(schema, "__required_keys__", ())),
        "optional": sorted(getattr(schema, "__optional_keys__", ())),
        "revision_bound": True,
    }


def middleware_contract(item, *, model, backend, control, children, deep):
    kind = qualified(item)
    if deep:
        from deepagents.middleware.memory import MemoryMiddleware

        from openeight.deepagents._frameworks._measurement import middleware_payload
        from openeight.deepagents._middleware import GovernanceBoundary

        if type(item) is GovernanceBoundary:
            if item.controller is not control or item._backend is not backend:
                raise ConfigurationError("unsupported_graph_shape")
            return {
                "path": list(control.child_path),
                "sources": list(item.sources),
                "source_labels": item.source_labels,
                "system_prompt_template": item.system_prompt_template,
                "interrupt_on": interrupt_contract(item._approvals.interrupt_on),
                "description_prefix": item._approvals.description_prefix,
                "edit_notice": item._approvals.edit_notice,
                "control_kind": qualified(control),
                "observed": control.observed,
            }
        if type(item) is MemoryMiddleware:
            if item._backend is not backend:
                raise ConfigurationError("unsupported_graph_shape")
            return {
                "sources": list(item.sources),
                "system_prompt": item.system_prompt,
                "add_cache_control": item._add_cache_control,
            }
        if kind.startswith(("deepagents.", "langchain_anthropic.middleware.")):
            return middleware_payload(
                item,
                model=model,
                backend=backend,
                control=control,
                child_paths=children,
                ordinary=True,
            )
    from langchain.agents.middleware import HumanInTheLoopMiddleware, TodoListMiddleware

    from openeight.langchain._controller import AttemptController

    if type(item) is HumanInTheLoopMiddleware:
        return {
            "interrupt_on": interrupt_contract(item.interrupt_on),
            "description_prefix": item.description_prefix,
            "edit_notice": item.edit_notice,
        }
    if type(item) is TodoListMiddleware:
        return {
            "system_prompt": item.system_prompt,
            "tool_description": item.tool_description,
        }
    if type(item) is AttemptController:
        if item is not control:
            raise ConfigurationError("unsupported_graph_shape")
        return {
            "path": list(control.child_path),
            "observed": control.observed,
            "on_block": control.on_block,
        }
    from langchain.agents.middleware import AgentMiddleware

    hooks = [
        name
        for name in (
            "before_agent",
            "abefore_agent",
            "before_model",
            "abefore_model",
            "after_model",
            "aafter_model",
            "after_agent",
            "aafter_agent",
            "wrap_model_call",
            "awrap_model_call",
            "wrap_tool_call",
            "awrap_tool_call",
        )
        if getattr(type(item), name) is not getattr(AgentMiddleware, name)
    ]
    return {"class": kind, "hooks": hooks, "revision_bound": True}


def interrupt_contract(settings):
    return {
        name: {
            key: callable_contract(value) if callable(value) else value
            for key, value in config.items()
        }
        for name, config in settings.items()
    }


def native_hitl_nodes(stacks, *, deep=False):
    from langchain.agents.middleware import HumanInTheLoopMiddleware

    allowed = (HumanInTheLoopMiddleware,)
    if deep:
        from openeight.deepagents._middleware import GovernanceBoundary

        allowed = (*allowed, GovernanceBoundary)
    return freeze(
        {
            path: [
                item.name + ".after_model" for item in items if type(item) in allowed
            ]
            for path, items in stacks.items()
        }
    )


def capture_nodes(
    *,
    graphs,
    stacks,
    bindings,
    models,
    controls,
    backend=None,
    response_spec=None,
    variant_name=None,
    deep=False,
):
    nodes = []
    for path, graph in sorted(graphs.items()):
        fields = getclosurevars(graph.nodes["model"].bound.afunc).nonlocals
        model, source, original, configuration_sha256 = models[path]
        if fields["model"] is not model:
            raise ConfigurationError("unsupported_graph_shape")
        children = [
            child
            for child in graphs
            if len(child) == len(path) + 1 and child[:-1] == path
        ]
        prompt = fields["system_message"]
        tools = []
        visible = {tool.name: tool for tool in fields["default_tools"]}
        for (tool_path, name), binding in sorted(bindings.native.items()):
            if tool_path != path:
                continue
            if visible.get(name) is not binding.tool:
                raise ConfigurationError("unsupported_graph_shape")
            tools.append(
                {
                    "native_name": name,
                    "catalog_name": binding.facts.name,
                    "input_schema_sha256": hashlib.sha256(
                        binding.schema_bytes
                    ).hexdigest(),
                    "executable_description_sha256": payload_hash(
                        binding.tool.description
                    ),
                    "model_visible_description_sha256": payload_hash(
                        visible[name].description
                    ),
                }
            )
        nodes.append(
            {
                "path": list(path),
                "source": source,
                "model_class": qualified(model),
                "model_spec_sha256": payload_hash(original)
                if original is not None
                else None,
                "system_message_sha256": payload_hash(
                    prompt.model_dump(mode="json") if prompt else None
                ),
                "middleware": [
                    {
                        "kind": qualified(item),
                        "configuration_sha256": payload_hash(
                            middleware_contract(
                                item,
                                model=model,
                                backend=backend,
                                control=controls[path],
                                children=children,
                                deep=deep,
                            )
                        ),
                    }
                    for item in stacks[path]
                ],
                "tools": tools,
                "delegates_to": [list(child) for child in children],
                "state_schema_kind": qualified(graph.builder.state_schema),
                "state_schema_sha256": payload_hash(
                    state_contract(graph.builder.state_schema)
                ),
                "private_state_keys": sorted(
                    getattr(
                        next(
                            (
                                item
                                for item in stacks[path]
                                if type(item).__name__ == "SubAgentMiddleware"
                            ),
                            None,
                        ),
                        "_private_state_keys",
                        (),
                    )
                ),
                "default_config_sha256": payload_hash(graph.config or {}),
                "runtime_variant": {
                    "name": variant_name,
                    "configuration_sha256": configuration_sha256,
                }
                if variant_name is not None
                else None,
                "response_format_sha256": response_spec.schema_sha256
                if response_spec is not None and not path
                else None,
            }
        )
    return freeze(nodes)


def configuration_digest(session, graph_revision):
    import platform
    from importlib.metadata import version

    from openeight._framework._measurement import plain

    variants = []
    backend = None
    for name, built in session._generation_builds.items():
        if (
            built.verify_configuration is None
            or "checkpoint_nodes" not in built.composition
        ):
            raise ConfigurationError("unsupported_graph_shape")
        nodes = built.verify_configuration()
        if nodes != built.composition["checkpoint_nodes"]:
            raise ConfigurationError("unsupported_graph_shape")
        current_backend = built.composition.get(
            "checkpoint_backend", {"kind": "none", "revision_bound": False}
        )
        if backend is not None and backend != current_backend:
            raise ConfigurationError("unsupported_graph_shape")
        backend = current_backend
        variants.append(
            {
                "name": name if session._runtime_models is not None else None,
                "nodes": plain(nodes),
            }
        )
    packages = dict(session._compatibility_selection.installed.packages)
    packages["python"] = platform.python_version()
    saver_packages = (
        ("langgraph-checkpoint-postgres", "psycopg", "psycopg-pool")
        if session._saver.kind == "async-postgres"
        else ("langgraph-checkpoint-sqlite", "aiosqlite")
    )
    for name in saver_packages:
        packages[name] = version(name)
    packages["adapter_revision"] = (
        session._compatibility_selection.implementation_revision
    )
    return payload_hash(
        {
            "format_version": 1,
            "purpose": "ordinary-checkpoint",
            "integration": session._integration,
            "graph_revision": graph_revision,
            "packages": packages,
            "saver": {
                "kind": session._saver.kind,
                "checkpoint_format": "langgraph-checkpoint-v4",
                "serializer": qualified(session._saver.serde),
                "pickle_fallback": False,
            },
            "backend": plain(backend),
            "variants": variants,
        }
    )
