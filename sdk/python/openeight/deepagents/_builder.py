"""Compose public Deep Agents construction hooks before admitting execution."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from openeight import ConfigurationError, Governance
from openeight._framework import (
    detect_framework,
    load_compatibility,
    select_compatibility,
)
from openeight._framework._measurement import payload_hash
from openeight._framework._runtime_models import (
    ModelInput,
    ResolvedInvocationModel,
    model_is_configurable,
    preparing_models,
    selector_values,
    snapshot_model,
)
from openeight._framework._structured_output import (
    ResponseFormatInput,
    inspect_response_format,
    validate_durable_response_format,
    validate_response_bindings,
)
from openeight._framework._types import FrameworkBuildRequest
from openeight._managed_types import ManagedResourceFactory, ManagedResources
from openeight.compatibility import inspect_inputs
from openeight.configuration import (
    OpenEightConfig,
    OpenEightConfigDict,
    normalize_config,
)
from openeight.langchain._controller import AttemptController
from openeight.langchain._managed import (
    DurableAgent,
    LocalDurableAgent,
    ManagedAgent,
    make_managed_agent,
    safe_diagnostics,
)
from openeight.langchain._session import _SessionOwner
from openeight.langchain.durable import DurableApprovalConfig

from ._compat import validate_versions
from ._frameworks._resolution import (
    capture_declarations,
    normalized_declarations,
    resolve_topology,
)
from ._permissions import copy_permissions, has_native_interrupts
from ._surface import (
    descriptions_map,
    invalid,
    surface_map,
    validate_qualified_path,
)
from .durable import DeepAgentsDeploymentConfig, LocalDurability

if TYPE_CHECKING:
    from langchain_core.messages import SystemMessage
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.store.base import BaseStore

    from ._surface import CatalogDescription, ToolSurface


_RESERVED = {
    "SkillsMiddleware",
    "HumanInTheLoopMiddleware",
    "SubAgentMiddleware",
    "AsyncSubAgentMiddleware",
    "FilesystemMiddleware",
    "PatchToolCallsMiddleware",
    "SummarizationMiddleware",
    "GovernanceBoundary",
    "AttemptController",
}
_CHILD_KEYS = {
    "name",
    "description",
    "system_prompt",
    "model",
    "tools",
    "middleware",
    "skills",
    "interrupt_on",
    "permissions",
}


def middleware_list(items, *, nested=False):
    items = tuple(items)
    from deepagents.middleware.subagents import SubAgentMiddleware

    if any(
        not isinstance(item, AgentMiddleware)
        or item.name in _RESERVED
        and not (nested and type(item) is SubAgentMiddleware)
        for item in items
    ):
        raise ConfigurationError("unsupported_graph_shape")
    if len({item.name for item in items}) != len(items):
        raise ConfigurationError("unsupported_graph_shape")
    return items


def validate_tool_names(tools, middleware):
    from deepagents.middleware.subagents import SubAgentMiddleware

    names = [
        item.name if isinstance(item, BaseTool) else getattr(item, "__name__", None)
        for item in tools
    ]
    names.extend(
        tool.name
        for item in middleware
        if type(item) is not SubAgentMiddleware
        for tool in getattr(item, "tools", ())
    )
    generated = {
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "delete",
        "glob",
        "grep",
        "execute",
        "task",
    }
    if (
        None in names
        or len(names) != len(set(names))
        or generated.intersection(names)
        or any(name.startswith(("deep_", "mcp_")) for name in names)
    ):
        raise ConfigurationError("unsupported_tool_definition")


def create_deep_agent(
    model: ModelInput,
    tools: Sequence[BaseTool | Callable[..., Any]] = (),
    *,
    openeight: OpenEightConfig | OpenEightConfigDict,
    middleware: Sequence[AgentMiddleware] = (),
    system_prompt: str | SystemMessage | None = None,
    backend: Any = None,
    permissions: Sequence[FilesystemPermission] | None = None,
    skills: Sequence[str] | None = None,
    memory: Sequence[str] | None = None,
    subagents: Sequence[Mapping[str, Any]] = (),
    interrupt_on: dict[str, bool | dict[str, Any]] | None = None,
    state_schema: type[Any] | None = None,
    context_schema: type[Any] | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    store: BaseStore | None = None,
    response_format: ResponseFormatInput = None,
    name: str | None = None,
    tool_surfaces: Sequence[ToolSurface] | None = None,
    catalog_naming: str = "hashed-v1",
    catalog_descriptions: Sequence[CatalogDescription] = (),
    include_general_purpose: bool | None = None,
    resources: ManagedResourceFactory | None = None,
    durable_deployment: DeepAgentsDeploymentConfig | None = None,
    durability: LocalDurability | None = None,
) -> ManagedAgent | DurableAgent | LocalDurableAgent:
    """Build a governed agent with the selected ordinary or durable lifecycle.

    Configuration is validated now; resources open on entry or invocation.
    Unsupported compositions raise ConfigurationError before tool execution.
    """
    return _context(
        model,
        tools,
        config=normalize_config(openeight, environ=os.environ, observed=False),
        middleware=middleware,
        system_prompt=system_prompt,
        backend=backend,
        permissions=permissions,
        skills=skills,
        memory=memory,
        subagents=subagents,
        interrupt_on=interrupt_on,
        state_schema=state_schema,
        context_schema=context_schema,
        checkpointer=checkpointer,
        store=store,
        response_format=response_format,
        name=name,
        tool_surfaces=tool_surfaces,
        catalog_naming=catalog_naming,
        catalog_descriptions=catalog_descriptions,
        include_general_purpose=include_general_purpose,
        resources=resources,
        durable_deployment=durable_deployment,
        durability=durability,
    )


def _validate_execution_mode(
    *,
    model,
    governance,
    resources,
    checkpointer,
    store,
    durability,
    durable_deployment,
    observed,
    backend,
    state_schema,
    context_schema,
    interrupt_on,
    name,
):
    if resources is not None and (
        not callable(resources) or checkpointer is not None or store is not None
    ):
        raise ConfigurationError("integration_resource_conflict")
    if (
        not isinstance(governance, Governance)
        or not (isinstance(model, (str, BaseChatModel)) or model_is_configurable(model))
        or isinstance(model, str)
        and not model.strip()
    ):
        raise ConfigurationError("invalid_integration_configuration")
    durable = governance.durable is not None or durability is not None
    if (
        governance.durable is not None
        and governance.durable.deployment_binding is not None
    ):
        invalid()
    if durability is not None:
        if (
            not isinstance(durability, LocalDurability)
            or governance.durable is not None
            or governance.approval is not None
            or durable_deployment is not None
            or observed
            or governance.sources
            or governance.mcp_tools is not None
            or governance.discovery is not None
        ):
            raise ConfigurationError("invalid_integration_configuration")
        if resources is not None or checkpointer is not None or store is not None:
            raise ConfigurationError("integration_resource_conflict")
        if backend is not None:
            raise ConfigurationError("unsupported_graph_shape")
    elif governance.durable is not None:
        if (
            not isinstance(governance.durable, DurableApprovalConfig)
            or not isinstance(durable_deployment, DeepAgentsDeploymentConfig)
            or observed
        ):
            raise ConfigurationError("invalid_integration_configuration")
        if resources is None and checkpointer is None:
            raise ConfigurationError("unsupported_checkpointer")
    elif durable_deployment is not None:
        raise ConfigurationError("invalid_integration_configuration")
    if durable and any(
        option is not None
        for option in (state_schema, context_schema, interrupt_on, name)
    ):
        raise ConfigurationError("unsupported_graph_shape")
    if governance.sources and checkpointer is not None:
        raise ConfigurationError("unsupported_checkpointer")
    return durable


def _normalize_children(subagents, *, tools, durable):
    children = []
    for child in subagents:
        if (
            type(child) is not dict
            or set(child) - _CHILD_KEYS
            or not {"name", "description"} <= set(child)
        ):
            raise ConfigurationError("unsupported_graph_shape")
        child = dict(child)
        if "permissions" in child:
            child["permissions"] = copy_permissions(child["permissions"])
        if durable and child.get("interrupt_on") is not None:
            raise ConfigurationError("unsupported_graph_shape")
        if (
            type(child["name"]) is not str
            or not child["name"]
            or len(child["name"].encode()) > 256
        ):
            raise ConfigurationError("unsupported_graph_shape")
        child["middleware"] = middleware_list(
            child.get("middleware", ()), nested=durable
        )
        child["tools"] = tuple(child.get("tools", tools))
        child["skills"] = tuple(child.get("skills", ()))
        validate_tool_names(child["tools"], child["middleware"])
        children.append(child)
    if len({child["name"] for child in children}) != len(children):
        raise ConfigurationError("unsupported_graph_shape")
    return children


def _nested_paths(children, *, model, tools, durable):
    from deepagents.middleware.subagents import SubAgentMiddleware

    from ._frameworks import _nested

    result = []

    def visit(spec, path):
        if len(path) > 16 or len(result) >= 200:
            raise ConfigurationError("unsupported_graph_shape")
        result.append(path)
        declared = spec.get("model", model)
        if (
            not (
                isinstance(declared, (str, BaseChatModel))
                or model_is_configurable(declared)
            )
            or isinstance(declared, str)
            and not declared.strip()
        ):
            raise ConfigurationError("invalid_integration_configuration")
        stack = middleware_list(spec.get("middleware", ()), nested=durable)
        validate_tool_names(spec.get("tools", tools), stack)
        for item in stack:
            if type(item) is SubAgentMiddleware:
                for descendant in _nested.validate(item):
                    visit(descendant, (*path, descendant["name"]))

    for spec in children:
        visit(spec, (spec["name"],))
    return result


def _validate_interrupts(
    children,
    *,
    permissions,
    interrupt_on,
    durable,
    checkpointer,
    resources,
    include_general_purpose,
):
    effective_rules = [
        permissions or (),
        *(child.get("permissions", permissions) or () for child in children),
    ]
    has_permission_interrupts = any(
        rule.mode == "interrupt" for rules in effective_rules for rule in rules
    )
    if has_permission_interrupts and durable:
        raise ConfigurationError("unsupported_checkpointer")
    if checkpointer is None and resources is None:
        declarations = [
            (permissions, interrupt_on),
            *[
                (
                    child.get("permissions", permissions),
                    child.get("interrupt_on", interrupt_on),
                )
                for child in children
            ],
        ]
        if any(
            has_native_interrupts(rules, settings) for rules, settings in declarations
        ):
            raise ConfigurationError("unsupported_checkpointer")
    if include_general_purpose is False and any(
        c["name"] == "general-purpose" for c in children
    ):
        invalid()


async def _prepare_session_models(
    *,
    model,
    tools,
    prepared,
    session_paths,
    captured_models,
    selector_keys,
    runtime_models,
    configurable,
    include_general_purpose,
    catalog_naming,
    surfaces,
    governance,
):
    with preparing_models():
        if prepared["topologies"] is not None:
            raise ConfigurationError("invalid_integration_configuration")
        topologies, records, cache = {}, {}, {}
        variants = runtime_models.variants if configurable else (None,)
        for variant in variants:
            selectors = (
                selector_values({"configurable": variant.configurable}, selector_keys)
                if variant
                else {}
            )
            signature = payload_hash(
                normalized_declarations(captured_models, selectors)
            )
            if signature in prepared["selectors"]:
                raise ConfigurationError("invalid_integration_configuration")
            request = FrameworkBuildRequest(
                model,
                tools,
                MappingProxyType(
                    {
                        "captured_models": captured_models,
                        "resolution_cache": cache,
                        "model_selectors": selectors,
                        "include_general_purpose": include_general_purpose,
                    }
                ),
                MappingProxyType({}),
                None,
                None,
                variant_name=variant.name if variant else None,
            )
            topology = await resolve_topology(request)
            topologies[variant.name if variant else None] = topology
            if variant:
                root = topology.nodes[()]
                record = ResolvedInvocationModel(
                    variant.name,
                    root.model,
                    root.profile,
                    root.configuration_sha256,
                )
                records[variant.name] = record
                prepared["selectors"][signature] = record
        session_paths[:] = list(
            dict.fromkeys(
                path for topology in topologies.values() for path in topology.nodes
            )
        )
        if catalog_naming == "qualified-v1":
            for path in session_paths:
                validate_qualified_path(path)
        if surfaces is not None and set(surfaces) != set(session_paths):
            invalid()
        prepared["topologies"], prepared["governance"] = (
            MappingProxyType(topologies),
            governance,
        )
        return MappingProxyType(records)


def _context(
    model,
    tools,
    *,
    config,
    middleware=(),
    system_prompt=None,
    backend=None,
    permissions: Sequence[FilesystemPermission] | None = None,
    skills=None,
    memory=None,
    subagents=(),
    interrupt_on: dict[str, bool | dict[str, Any]] | None = None,
    state_schema=None,
    context_schema=None,
    checkpointer=None,
    store=None,
    response_format: ResponseFormatInput = None,
    name=None,
    tool_surfaces: Sequence[ToolSurface] | None = None,
    catalog_naming="hashed-v1",
    catalog_descriptions: Sequence[CatalogDescription] = (),
    include_general_purpose=None,
    resources: ManagedResourceFactory | None = None,
    durable_deployment: DeepAgentsDeploymentConfig | None = None,
    durability: LocalDurability | None = None,
    observed=False,
):
    governance = config.governance
    permissions = copy_permissions(permissions)
    durable = _validate_execution_mode(
        model=model,
        governance=governance,
        resources=resources,
        checkpointer=checkpointer,
        store=store,
        durability=durability,
        durable_deployment=durable_deployment,
        observed=observed,
        backend=backend,
        state_schema=state_schema,
        context_schema=context_schema,
        interrupt_on=interrupt_on,
        name=name,
    )
    required = {"managed_integrations", "durable" if durable else "ordinary"}
    if durability is not None:
        required.add("local_durable")
    selection = select_compatibility(
        detect_framework("deepagents"), required=frozenset(required)
    )
    validate_versions()
    safe_diagnostics()
    response_spec = inspect_response_format(response_format)
    response_format = response_spec.native if response_spec else None
    if durable:
        validate_durable_response_format(response_spec)
    surfaces = surface_map(tool_surfaces)
    descriptions = descriptions_map(catalog_descriptions)
    if catalog_naming not in ("hashed-v1", "qualified-v1") or (
        include_general_purpose is not None
        and type(include_general_purpose) is not bool
    ):
        invalid()
    if catalog_naming == "qualified-v1" and (
        governance.sources or governance.mcp_tools
    ):
        invalid()
    root_middleware = middleware_list(middleware)
    tools, skills, memory = tuple(tools), tuple(skills or ()), tuple(memory or ())
    validate_tool_names(tools, root_middleware)
    children = _normalize_children(subagents, tools=tools, durable=durable)

    child_paths = _nested_paths(children, model=model, tools=tools, durable=durable)
    _validate_interrupts(
        children,
        permissions=permissions,
        interrupt_on=interrupt_on,
        durable=durable,
        checkpointer=checkpointer,
        resources=resources,
        include_general_purpose=include_general_purpose,
    )

    configurable = model_is_configurable(model) or any(
        model_is_configurable(child.get("model", model)) for child in children
    )
    if configurable != (config.runtime_models is not None):
        raise ConfigurationError("invalid_integration_configuration")
    if durable and configurable and len(config.runtime_models.variants) != 1:
        raise ConfigurationError("invalid_integration_configuration")
    enrolled_keys = (
        {
            key
            for variant in config.runtime_models.variants
            for key in variant.configurable
        }
        if configurable
        else set()
    )
    model = snapshot_model(model, enrolled_keys=enrolled_keys, deep=True)
    for child in children:
        if "model" in child:
            child["model"] = snapshot_model(
                child["model"], enrolled_keys=enrolled_keys, deep=True
            )

    paths = [(), *child_paths]
    cls = (
        LocalDurableAgent
        if durability is not None
        else DurableAgent
        if durable
        else _SessionOwner
    )

    def session_factory():
        captured_models = capture_declarations(
            model, children, enrolled_keys=enrolled_keys
        )
        selector_keys = frozenset(
            key for entry in captured_models.values() for key in entry[2]
        )
        compatibility = load_compatibility(selection)
        prepared = {"topologies": None, "governance": None, "selectors": {}}
        session_paths = list(paths)

        async def prepare(*, resources, governance):
            return await _prepare_session_models(
                model=model,
                tools=tools,
                prepared=prepared,
                session_paths=session_paths,
                captured_models=captured_models,
                selector_keys=selector_keys,
                runtime_models=config.runtime_models,
                configurable=configurable,
                include_general_purpose=include_general_purpose,
                catalog_naming=catalog_naming,
                surfaces=surfaces,
                governance=governance,
            )

        def select_model(config):
            if not configurable or prepared["topologies"] is None:
                raise ConfigurationError("invalid_integration_configuration")
            selectors = selector_values(config, selector_keys)
            record = prepared["selectors"].get(
                payload_hash(normalized_declarations(captured_models, selectors))
            )
            if record is None:
                raise ConfigurationError("invalid_integration_configuration")
            return record

        def build(saver, remotes, *, resources, governance=None, variant_name=None):
            if (
                prepared["topologies"] is None
                or governance is not None
                and governance != prepared["governance"]
            ):
                raise ConfigurationError("invalid_integration_configuration")
            topology = prepared["topologies"].get(variant_name)
            if topology is None:
                raise ConfigurationError("invalid_integration_configuration")
            governance = prepared["governance"]
            if (
                _nested_paths(children, model=model, tools=tools, durable=durable)
                != child_paths
            ):
                raise ConfigurationError("unsupported_graph_shape")
            validate_tool_names(tools, root_middleware)
            for child in children:
                validate_tool_names(child["tools"], child["middleware"])
            request = FrameworkBuildRequest(
                model,
                tools,
                MappingProxyType(
                    {
                        "root_middleware": root_middleware,
                        "checkpoints": config.checkpoints is not None,
                        "children": children,
                        "surfaces": {path: surfaces[path] for path in topology.nodes}
                        if surfaces is not None
                        else None,
                        "descriptions": descriptions,
                        "catalog_naming": catalog_naming,
                        "include_general_purpose": include_general_purpose,
                        "governance": governance,
                        "observed": observed,
                        "interrupt_on": interrupt_on,
                        "system_prompt": system_prompt,
                        "backend": backend,
                        "permissions": permissions,
                        "skills": skills,
                        "memory": memory,
                        "state_schema": state_schema,
                        "context_schema": context_schema,
                        "response_format": response_format,
                        "name": name,
                        "remotes": remotes,
                    }
                ),
                MappingProxyType(
                    {
                        path: AttemptController(governance, path, observed=observed)
                        for path in topology.nodes
                    }
                ),
                resources.checkpointer if governance.durable is not None else saver,
                resources.store,
                resolved=topology,
                variant_name=variant_name,
                response_spec=response_spec,
            )
            failed = False
            built = None
            try:
                built = compatibility.build(request)
                compatibility.inspect(built)
                validate_response_bindings(built, response_spec)
            except ConfigurationError:
                raise
            except Exception:
                failed = True
            if failed:
                raise ConfigurationError("invalid_integration_configuration")
            return built

        return cls(
            build,
            governance,
            prepare=prepare,
            select_model=select_model,
            required_features={"managed_integrations"},
            runtime_models=config.runtime_models,
            checkpoints=config.checkpoints,
            integration="deepagents",
            observed=observed,
            resources=resources,
            compatibility=compatibility,
            borrowed_resources=ManagedResources(checkpointer, store),
            paths=session_paths,
            durable_deployment=durable_deployment,
            durability=durability,
        )

    def inspect_compatibility():
        return inspect_inputs(
            model=model,
            governance=governance,
            resources=resources,
            checkpointer=checkpointer,
            middleware=middleware,
            state_schema=state_schema,
            context_schema=context_schema,
        )

    if durable:
        return session_factory()
    return make_managed_agent(
        session_factory=session_factory,
        inspect_compatibility=inspect_compatibility,
        checkpointed=checkpointer is not None or resources is not None,
    )


def observe(
    model: ModelInput,
    tools: Sequence[BaseTool | Callable[..., Any]] = (),
    *,
    openeight: OpenEightConfig | OpenEightConfigDict,
    middleware: Sequence[AgentMiddleware] = (),
    system_prompt: str | SystemMessage | None = None,
    backend: Any = None,
    permissions: Sequence[FilesystemPermission] | None = None,
    skills: Sequence[str] | None = None,
    memory: Sequence[str] | None = None,
    subagents: Sequence[Mapping[str, Any]] = (),
    interrupt_on: dict[str, bool | dict[str, Any]] | None = None,
    state_schema: type[Any] | None = None,
    context_schema: type[Any] | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    store: BaseStore | None = None,
    response_format: ResponseFormatInput = None,
    name: str | None = None,
    tool_surfaces: Sequence[ToolSurface] | None = None,
    catalog_naming: str = "hashed-v1",
    catalog_descriptions: Sequence[CatalogDescription] = (),
    include_general_purpose: bool | None = None,
    resources: ManagedResourceFactory | None = None,
) -> ManagedAgent:
    """Build an observation-only agent; this adapter does not authorize tools.

    Resources open on entry or invocation and close with their owning session.
    """
    config = normalize_config(openeight, environ=os.environ, observed=True)
    return _context(
        model,
        tools,
        config=config,
        observed=True,
        resources=resources,
        middleware=middleware,
        system_prompt=system_prompt,
        backend=backend,
        permissions=permissions,
        skills=skills,
        memory=memory,
        subagents=subagents,
        interrupt_on=interrupt_on,
        state_schema=state_schema,
        context_schema=context_schema,
        checkpointer=checkpointer,
        store=store,
        response_format=response_format,
        name=name,
        tool_surfaces=tool_surfaces,
        catalog_naming=catalog_naming,
        catalog_descriptions=catalog_descriptions,
        include_general_purpose=include_general_purpose,
    )
