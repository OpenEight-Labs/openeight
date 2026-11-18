"""One native-primitive Deep assembly path for static and resolved models."""

from importlib.metadata import version
from types import MappingProxyType

from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT

from openeight import ConfigurationError
from openeight._framework._types import ManagedBuild
from openeight.deepagents._compat import capture, validate_versions
from openeight.deepagents._middleware import GovernanceBoundary, ModelSelectionGuard
from openeight.deepagents._surface import qualified_name, select_tools
from openeight.langchain._bindings import AdapterBindings


def _compile(
    *, request, resolved, specs, tools, root_middleware, backend, surfaces, stacks
):
    from deepagents._tools import _apply_tool_description_overrides
    from deepagents.graph import DeepAgentState, _apply_custom_middleware
    from deepagents.middleware._prompt_caching import append_prompt_caching_middleware
    from deepagents.middleware._state import private_state_field_names
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from deepagents.middleware.memory import MemoryMiddleware
    from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
    from deepagents.middleware.subagents import SubAgentMiddleware
    from deepagents.middleware.summarization import create_summarization_middleware
    from deepagents.middleware.unsupported_content import UnsupportedContentMiddleware
    from deepagents.profiles.harness.harness_profiles import _apply_profile_prompt
    from langchain.agents import create_agent
    from langchain_core.messages import SystemMessage

    from openeight.deepagents._surface import FS_NAMES, invalid

    options = request.options
    profile = resolved.nodes[()].profile

    def core(model, profile, path, permissions):
        names = None if surfaces is None else surfaces[path] & FS_NAMES
        stack = []
        if names is None or names:
            if names is not None and "read_file" not in names:
                invalid()
            stack.append(
                FilesystemMiddleware(
                    backend=backend,
                    tools=None if names is None else sorted(names),
                    custom_tool_descriptions=profile.tool_description_overrides,
                    _permissions=list(permissions or ()),
                )
            )
        stack.extend(
            [
                create_summarization_middleware(model, backend),
                PatchToolCallsMiddleware(),
            ]
        )
        return stack

    children = []
    for raw in specs:
        child = dict(raw)
        node = resolved.nodes[(child["name"],)]
        child_profile = node.profile
        stack = core(
            node.model,
            child_profile,
            (child["name"],),
            child.get("permissions", options["permissions"]),
        )
        core_names = {item.name for item in stack}
        boundary = child["middleware"][-1]
        stack.append(boundary)
        append_prompt_caching_middleware(stack)
        stack.append(UnsupportedContentMiddleware())
        custom = list(child["middleware"])
        if node.source == "root-general-purpose":
            native_slots = {
                item.name
                for item in stack
                if type(item) is not UnsupportedContentMiddleware
            }
            inherited = [
                item for item in root_middleware[:-1] if item.name in native_slots
            ]
            custom = [*inherited, *custom]
        stack = _apply_custom_middleware(stack, custom, core_names=core_names)
        stack.append(ModelSelectionGuard(boundary.controller))
        child["middleware"] = stack
        stacks[(child["name"],)] = tuple(stack)
        child["tools"] = _apply_tool_description_overrides(
            child["tools"], child_profile.tool_description_overrides
        )
        gp = child_profile.general_purpose_subagent
        if (
            node.source == "root-general-purpose"
            and gp is not None
            and gp.system_prompt is not None
        ):
            prompt = gp.system_prompt
            if child_profile.system_prompt_suffix is not None:
                prompt += "\n\n" + child_profile.system_prompt_suffix
        else:
            prompt = _apply_profile_prompt(
                child_profile, child.get("system_prompt", "")
            )
        child["system_prompt"] = prompt
        child.pop("skills", None)
        child.pop("interrupt_on", None)
        child.pop("permissions", None)
        children.append(child)

    root = core(resolved.nodes[()].model, profile, (), options["permissions"])
    task = None
    if children:
        task = SubAgentMiddleware(
            backend=backend,
            subagents=children,
            state_schema=options["state_schema"],
            task_description=profile.tool_description_overrides.get("task"),
        )
        root.insert(1 if root and type(root[0]) is FilesystemMiddleware else 0, task)
    core_names = {item.name for item in root}
    root.append(root_middleware[-1])
    append_prompt_caching_middleware(root)
    if options["memory"]:
        root.append(
            MemoryMiddleware(
                backend=backend, sources=list(options["memory"]), add_cache_control=True
            )
        )
    root.append(UnsupportedContentMiddleware())
    root = _apply_custom_middleware(root, root_middleware, core_names=core_names)
    root.append(ModelSelectionGuard(root_middleware[-1].controller))
    stacks[()] = tuple(root)
    schemas = [options["state_schema"]] if options["state_schema"] is not None else []
    schemas.extend(item.state_schema for item in root if item.state_schema is not None)
    private_keys = private_state_field_names(*schemas)
    if task is not None:
        task.private_state_keys = task.private_state_keys | private_keys
    base_prompt = _apply_profile_prompt(profile, "")
    prompt = options["system_prompt"]
    if prompt is None:
        prompt = base_prompt
    elif isinstance(prompt, SystemMessage):
        if base_prompt:
            prompt = SystemMessage(
                content_blocks=[
                    *prompt.content_blocks,
                    {"type": "text", "text": "\n\n" + base_prompt},
                ]
            )
    elif base_prompt:
        prompt += "\n\n" + base_prompt
    effective_tools = _apply_tool_description_overrides(
        [*tools, *(binding.tool for binding in options["remotes"].get((), ()))],
        profile.tool_description_overrides,
    )
    return create_agent(
        resolved.nodes[()].model,
        tools=effective_tools,
        middleware=root,
        system_prompt=prompt,
        state_schema=options["state_schema"] or DeepAgentState,
        context_schema=options["context_schema"],
        checkpointer=request.checkpointer,
        store=request.store,
        response_format=options["response_format"],
        name=options["name"],
        debug=False,
    ).with_config(
        {
            "recursion_limit": 9999,
            "metadata": {
                "ls_integration": "deepagents",
                "lc_versions": {"deepagents": version("deepagents")},
                "lc_agent_name": options["name"],
            },
        }
    )


def _durable_surfaces(surfaces, tools, root_middleware, specs):
    from deepagents.middleware.subagents import SubAgentMiddleware

    from . import _nested

    if surfaces is None:
        from openeight.deepagents._surface import FS_NAMES

        def names(items, middleware):
            return {getattr(t, "name", getattr(t, "__name__", None)) for t in items} | {
                t.name for m in middleware for t in m.tools
            }

        surfaces = {
            (): set(FS_NAMES) - {"execute"}
            | ({"task"} if specs else set())
            | names(tools, root_middleware)
        }
        surfaces.update(
            {
                (spec["name"],): set(FS_NAMES) - {"execute"}
                | names(spec.get("tools", tools), spec.get("middleware", ()))
                for spec in specs
            }
        )

        def nested_surfaces(spec, path):
            for item in spec.get("middleware", ()):
                if type(item) is SubAgentMiddleware:
                    for nested in _nested.children(item):
                        child_path = (*path, nested["name"])
                        surfaces[child_path] = names(
                            nested["tools"], nested.get("middleware", ())
                        )
                        nested_surfaces(nested, child_path)

        for spec in specs:
            nested_surfaces(spec, (spec["name"],))
    if any("execute" in selected for selected in surfaces.values()):
        raise ConfigurationError("unsupported_tool_definition")
    return surfaces


def _topology(graphs, bindings):
    from hashlib import sha256

    from openeight.deepagents._deployment import canonical

    topology = []
    for path in sorted(graphs, key=lambda p: canonical(list(p)).encode()):
        captured = [binding for (p, _), binding in bindings.native.items() if p == path]
        topology.append(
            {
                "path": list(path),
                "delegates_to": [
                    list(p)
                    for p in sorted(graphs)
                    if len(p) == len(path) + 1 and p[:-1] == path
                ],
                "tools": [
                    {
                        "native_name": b.native_name,
                        "catalog_name": b.facts.name,
                        "input_schema_sha256": sha256(b.schema_bytes).hexdigest(),
                    }
                    for b in sorted(captured, key=lambda b: b.native_name.encode())
                ],
            }
        )
    return topology


def _durable_verifier(
    *,
    request,
    resolved,
    graph,
    graphs,
    boundaries,
    surfaces,
    bindings,
    stacks,
    backend,
    composition,
):
    from openeight._framework._structured_output import response_configuration

    from ._inspection import native_graphs
    from ._measurement import capture_configuration, dispatch_signature

    signatures = {path: dispatch_signature(value) for path, value in graphs.items()}
    response_signature = response_configuration(graph)

    def verify():
        if response_configuration(graph) != response_signature:
            raise ConfigurationError("unsupported_graph_shape")
        current_tools = capture(graph, boundaries, surfaces)
        for path, tools_at_path in current_tools.items():
            expected_tools = {
                name: binding.tool
                for (tool_path, name), binding in bindings.native.items()
                if tool_path == path
            }
            if {tool.name: tool for tool in tools_at_path} != expected_tools or any(
                tool is not expected_tools[tool.name] for tool in tools_at_path
            ):
                raise ConfigurationError("unsupported_graph_shape")
        bindings.validate()
        current_graphs = native_graphs(graph, request.controls)
        if set(current_graphs) != set(graphs) or any(
            current_graphs[path] is not graphs[path] for path in graphs
        ):
            raise ConfigurationError("unsupported_graph_shape")
        if any(
            dispatch_signature(value) != signatures[path]
            for path, value in current_graphs.items()
        ):
            raise ConfigurationError("unsupported_graph_shape")
        return capture_configuration(
            graphs=graphs,
            resolved=resolved,
            stacks=stacks,
            bindings=bindings,
            controls=request.controls,
            backend=backend,
            composition=composition,
            response_spec=request.response_spec,
            variant_name=request.variant_name,
        )

    measured = verify()
    composition["effective_configuration"] = measured.descriptor
    composition["effective_configuration_sha256"] = measured.sha256
    return verify


def _checkpoint_verifier(
    *,
    request,
    resolved,
    graph,
    graphs,
    boundaries,
    surfaces,
    bindings,
    stacks,
    backend,
    composition,
):
    from openeight._framework._dispatch import dispatch_signature
    from openeight.langchain._checkpoint_measurement import (
        capture_nodes,
        native_hitl_nodes,
    )

    from ._inspection import native_graphs

    signatures = {path: dispatch_signature(value) for path, value in graphs.items()}

    def verify():
        current = native_graphs(graph, boundaries)
        if set(current) != set(graphs) or any(
            current[path] is not graphs[path]
            or dispatch_signature(current[path]) != signatures[path]
            for path in graphs
        ):
            raise ConfigurationError("unsupported_graph_shape")
        capture(graph, boundaries, surfaces)
        bindings.validate()
        return capture_nodes(
            graphs=graphs,
            stacks=stacks,
            bindings=bindings,
            models={
                path: (
                    node.model,
                    node.source,
                    node.original_model_string,
                    node.configuration_sha256,
                )
                for path, node in resolved.nodes.items()
            },
            controls=request.controls,
            backend=backend,
            response_spec=request.response_spec,
            variant_name=request.variant_name,
            deep=True,
        )

    composition["checkpoint_nodes"] = verify()
    composition["checkpoint_hitl_nodes"] = native_hitl_nodes(stacks, deep=True)
    from openeight._framework._measurement import qualified

    composition["checkpoint_backend"] = {
        "kind": qualified(backend),
        "revision_bound": True,
    }
    return verify


def _child_specs(request, resolved):
    children = request.options["children"]
    include_general_purpose = request.options["include_general_purpose"]
    model, tools = resolved.nodes[()].model, request.tools
    skills = request.options["skills"]

    def wants_default(gp):
        return (
            include_general_purpose
            if include_general_purpose is not None
            else gp is None or gp.enabled is not False
        )

    profile = resolved.nodes[()].profile
    specs = [dict(child) for child in children]
    gp = profile.general_purpose_subagent
    if wants_default(gp) and not any(c["name"] == "general-purpose" for c in specs):
        default = {
            **GENERAL_PURPOSE_SUBAGENT,
            "model": model,
            "tools": tools,
            "skills": skills,
            "middleware": (),
        }
        if gp is not None:
            if gp.description is not None:
                default["description"] = gp.description
            if gp.system_prompt is not None:
                default["system_prompt"] = gp.system_prompt
        specs.insert(0, default)
    configuration_specs = tuple(
        {
            key: spec.get(key)
            for key in (
                "name",
                "description",
                "system_prompt",
                "middleware",
                "skills",
            )
        }
        for spec in specs
    )
    return specs, configuration_specs


def assemble_deep(request, resolved):
    from deepagents.graph import _merge_fs_interrupt_on
    from deepagents.middleware._fs_interrupt import _build_interrupt_on_from_permissions

    model, tools = resolved.nodes[()].model, request.tools
    root_middleware = request.options["root_middleware"]
    surfaces = request.options["surfaces"]
    descriptions = request.options["descriptions"]
    catalog_naming = request.options["catalog_naming"]
    governance = request.options["governance"]
    interrupt_on = request.options["interrupt_on"]
    permissions = request.options["permissions"]
    system_prompt = request.options["system_prompt"]
    backend = request.options["backend"]
    durable = governance.durable is not None
    skills = request.options["skills"]
    memory = request.options["memory"]
    remotes = request.options["remotes"]
    saver = request.checkpointer

    validate_versions()
    specs, configuration_specs = _child_specs(request, resolved)
    if durable:
        surfaces = _durable_surfaces(surfaces, tools, root_middleware, specs)
    controls, boundaries = [], {}
    stacks = {}

    def boundary(path, sources, approval):
        control = request.controls[path]
        control._managed_model = resolved.nodes[path].model
        controls.append(control)
        result = GovernanceBoundary(
            control, sources=sources, backend=backend, interrupt_on=approval
        )
        boundaries[path] = result
        return result

    root_approval = _merge_fs_interrupt_on(
        _build_interrupt_on_from_permissions(list(permissions or ())), interrupt_on
    )
    root_boundary = boundary((), skills, root_approval)

    def nested_stack(items, path):
        if not durable:
            return list(items)
        from deepagents.middleware.subagents import SubAgentMiddleware
        from deepagents.middleware.unsupported_content import (
            UnsupportedContentMiddleware,
        )
        from langchain.agents.middleware import TodoListMiddleware

        from . import _nested

        result = []
        for item in items:
            if type(item) is TodoListMiddleware:
                result.append(_nested.todo_copy(item))
            elif type(item) is SubAgentMiddleware:
                descendants = []
                for raw in _nested.validate(item):
                    spec = dict(raw)
                    child_path = (*path, spec["name"])
                    spec["tools"] = select_tools(
                        spec["tools"],
                        spec.get("middleware", ()),
                        surfaces[child_path],
                    )
                    spec["middleware"] = [
                        *nested_stack(spec.get("middleware", ()), child_path),
                        boundary(child_path, (), None),
                        UnsupportedContentMiddleware(),
                        ModelSelectionGuard(request.controls[child_path]),
                    ]
                    stacks[child_path] = tuple(spec["middleware"])
                    descendants.append(spec)
                result.append(_nested.reconstruct(item, descendants, backend=backend))
            else:
                raise ConfigurationError("unsupported_graph_shape")
        return result

    for child in specs:
        child["model"] = resolved.nodes[(child["name"],)].model
        approval = _merge_fs_interrupt_on(
            _build_interrupt_on_from_permissions(
                list(child.get("permissions", permissions) or ())
            ),
            child.get("interrupt_on", interrupt_on),
        )
        if approval and saver is None:
            raise ConfigurationError("unsupported_checkpointer")
        child.setdefault("model", model)
        if surfaces is not None:
            child["tools"] = select_tools(
                child["tools"], child["middleware"], surfaces[(child["name"],)]
            )
        child["middleware"] = [
            *nested_stack(child.get("middleware", ()), (child["name"],)),
            boundary((child["name"],), child.get("skills", ()), approval),
        ]
        child["interrupt_on"] = None
        child["skills"] = list(child.get("skills", ()))
        child["tools"] = [
            *child.get("tools", tools),
            *(b.tool for b in remotes.get((child["name"],), ())),
        ]
    effective_tools = (
        select_tools(tools, root_middleware, surfaces[()])
        if surfaces is not None
        else tools
    )
    effective_middleware = [*nested_stack(root_middleware, ()), root_boundary]
    graph = _compile(
        request=request,
        resolved=resolved,
        specs=specs,
        tools=effective_tools,
        root_middleware=effective_middleware,
        backend=backend,
        surfaces=surfaces,
        stacks=stacks,
    )
    from ._inspection import native_graphs

    graphs = MappingProxyType(native_graphs(graph, boundaries))
    from inspect import getclosurevars

    for path, native_graph in graphs.items():
        function = native_graph.nodes["model"].bound
        if (
            getclosurevars(function.func).nonlocals["model"]
            is not resolved.nodes[path].model
            or getclosurevars(function.afunc).nonlocals["model"]
            is not resolved.nodes[path].model
        ):
            raise ConfigurationError("unsupported_graph_shape")
    bindings = AdapterBindings(
        capture(graph, boundaries, surfaces),
        deep=True,
        name_mapper=qualified_name if catalog_naming == "qualified-v1" else None,
        descriptions=descriptions,
        remotes={id(b.tool): b for bindings in remotes.values() for b in bindings},
        max_tools=governance.discovery.max_tools if governance.discovery else 200,
        max_bytes=governance.discovery.max_catalog_bytes
        if governance.discovery
        else 1048576,
    )
    composition = {
        "topology": _topology(graphs, bindings),
        "backend": {"kind": "state", "format_version": 1},
        "saver": {
            "kind": "async-sqlite",
            "format_version": 1,
            "pickle_fallback": False,
        },
        "catalog_naming": catalog_naming,
        "configuration_sha256": None,
    }
    if request.variant_name is not None and not durable:
        composition["runtime_model_configuration"] = {
            "name": request.variant_name,
            "configuration_sha256": resolved.nodes[()].configuration_sha256,
        }
    if durable:
        from hashlib import sha256

        from ._configuration import local_configuration_descriptor

        descriptor = local_configuration_descriptor(
            constructor_options={
                "system_prompt": system_prompt,
                "children": configuration_specs,
                "skills": skills,
                "memory": memory,
                "middleware": root_middleware,
            },
            composition=composition,
        )
        composition["configuration_sha256"] = sha256(descriptor.encode()).hexdigest()
    verify = None
    if durable or request.options.get("checkpoints"):
        capture_verifier = _durable_verifier if durable else _checkpoint_verifier
        verify = capture_verifier(
            request=request,
            resolved=resolved,
            graph=graph,
            graphs=graphs,
            boundaries=boundaries,
            surfaces=surfaces,
            bindings=bindings,
            stacks=stacks,
            backend=backend,
            composition=composition,
        )
    from openeight._framework._measurement import freeze

    return ManagedBuild(graph, bindings, tuple(controls), freeze(composition), verify)
