"""Native LangChain construction with SDK-owned composition and lifecycle."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

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
    capture_model,
    model_is_configurable,
    normalize_captured,
    preparing_models,
    resolve_captured,
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
from openeight.langchain._managed import make_managed_agent
from openeight.langchain._session import _SessionOwner

from ._compat import validate_versions
from ._controller import AttemptController
from ._managed import DurableAgent, ManagedAgent, safe_diagnostics
from ._qualified import validate_managed_versions

if TYPE_CHECKING:
    from langchain_core.messages import SystemMessage
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.store.base import BaseStore


def create_agent(
    model: ModelInput,
    tools: Sequence[BaseTool | Callable[..., Any]] = (),
    *,
    openeight: OpenEightConfig | OpenEightConfigDict,
    middleware: Sequence[AgentMiddleware] = (),
    system_prompt: str | SystemMessage | None = None,
    state_schema: type[Any] | None = None,
    context_schema: type[Any] | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    store: BaseStore | None = None,
    response_format: ResponseFormatInput = None,
    name: str | None = None,
    resources: ManagedResourceFactory | None = None,
) -> ManagedAgent | DurableAgent:
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
        state_schema=state_schema,
        context_schema=context_schema,
        checkpointer=checkpointer,
        store=store,
        response_format=response_format,
        name=name,
        resources=resources,
    )


def _context(
    model,
    tools,
    *,
    config,
    middleware=(),
    system_prompt=None,
    state_schema=None,
    context_schema=None,
    checkpointer=None,
    store=None,
    response_format: ResponseFormatInput = None,
    name=None,
    resources: ManagedResourceFactory | None = None,
    observed=False,
):
    governance = config.governance
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
    selection = select_compatibility(
        detect_framework("langchain"),
        required=frozenset({"ordinary", "managed_integrations"})
        if governance.durable is None
        or governance.durable.deployment_binding is not None
        else frozenset({"ordinary"}),
    )
    safe_diagnostics()
    validate_versions()
    validate_managed_versions()
    response_spec = inspect_response_format(response_format)
    response_format = response_spec.native if response_spec else None
    if governance.sources and (
        checkpointer is not None or governance.durable is not None
    ):
        raise ConfigurationError("unsupported_checkpointer")
    middleware, tools = tuple(middleware), tuple(tools)
    if any(
        not isinstance(item, AgentMiddleware)
        or isinstance(item, AttemptController)
        or item.name in {"AttemptController", "OpenEightMiddleware"}
        for item in middleware
    ):
        raise ConfigurationError("unsupported_graph_shape")

    def validate_names():
        names = [
            item.name if isinstance(item, BaseTool) else getattr(item, "__name__", None)
            for item in tools
        ]
        names.extend(
            tool.name for item in middleware for tool in getattr(item, "tools", ())
        )
        if (
            None in names
            or len(set(names)) != len(names)
            or any(name.startswith(("deep_", "mcp_")) for name in names)
        ):
            raise ConfigurationError("unsupported_tool_definition")

    validate_names()
    durable = governance.durable is not None
    deployment_binding = governance.durable.deployment_binding if durable else None
    if (
        durable
        and deployment_binding is None
        and (
            isinstance(model, str)
            or response_spec is not None
            or config.runtime_models is not None
        )
    ):
        raise ConfigurationError("unsupported_execution_method")
    if (
        durable
        and config.runtime_models is not None
        and len(config.runtime_models.variants) != 1
    ):
        raise ConfigurationError("invalid_integration_configuration")
    if durable and (
        state_schema is not None
        or context_schema is not None
        or (checkpointer is None and resources is None)
    ):
        raise ConfigurationError("invalid_integration_configuration")

    if durable:
        validate_durable_response_format(response_spec)

    configurable = model_is_configurable(model)
    if configurable != (config.runtime_models is not None):
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
    model = snapshot_model(model, enrolled_keys=enrolled_keys)

    def session_factory():
        normalize_model, resolve_model, selector_keys = capture_model(
            model, enrolled_keys=enrolled_keys
        )
        compatibility = load_compatibility(selection)
        prepared = {"model": None, "records": None, "selectors": {}}

        async def prepare(*, resources, governance):
            with preparing_models():
                if prepared["records"] is not None or governance != config.governance:
                    raise ConfigurationError("invalid_integration_configuration")
                records = {}
                if configurable:
                    for variant in config.runtime_models.variants:
                        selectors = selector_values(
                            {"configurable": variant.configurable}, selector_keys
                        )
                        digest = payload_hash(
                            normalize_captured(normalize_model, selectors)
                        )
                        if digest in prepared["selectors"]:
                            raise ConfigurationError(
                                "invalid_integration_configuration"
                            )
                        actual, _ = resolve_captured(resolve_model, selectors)
                        record = ResolvedInvocationModel(
                            variant.name, actual, None, digest
                        )
                        records[variant.name] = record
                        prepared["selectors"][digest] = record
                else:
                    prepared["model"], _ = resolve_captured(resolve_model, {})
                prepared["records"] = MappingProxyType(records)
                return prepared["records"]

        def select_model(config):
            if not configurable or prepared["records"] is None:
                raise ConfigurationError("invalid_integration_configuration")
            selectors = selector_values(config, selector_keys)
            digest = payload_hash(normalize_captured(normalize_model, selectors))
            record = prepared["selectors"].get(digest)
            if record is None:
                raise ConfigurationError("invalid_integration_configuration")
            return record

        def build(scoped, remotes, *, resources, governance=None, variant_name=None):
            if (
                prepared["records"] is None
                or governance is not None
                and governance != config.governance
            ):
                raise ConfigurationError("invalid_integration_configuration")
            if configurable:
                selected = prepared["records"].get(variant_name)
                if selected is None:
                    raise ConfigurationError("invalid_integration_configuration")
            else:
                if variant_name is not None:
                    raise ConfigurationError("invalid_integration_configuration")
                selected = prepared["model"]
            governance = config.governance
            validate_names()
            control = AttemptController(governance, observed=observed)
            request = FrameworkBuildRequest(
                model,
                tools,
                MappingProxyType(
                    {
                        "middleware": middleware,
                        "system_prompt": system_prompt,
                        "state_schema": state_schema,
                        "context_schema": context_schema,
                        "response_format": response_format,
                        "name": name,
                        "durable": durable,
                        "checkpoints": config.checkpoints is not None,
                        "deployment_binding": deployment_binding is not None,
                        "governance": governance,
                        "remotes": remotes,
                    }
                ),
                MappingProxyType({(): control}),
                resources.checkpointer if durable else scoped,
                resources.store,
                resolved=selected,
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

        cls = DurableAgent if durable else _SessionOwner
        return cls(
            build,
            governance,
            prepare=prepare,
            select_model=select_model,
            required_features={"managed_integrations"}
            if not durable or deployment_binding is not None
            else (),
            runtime_models=config.runtime_models,
            checkpoints=config.checkpoints,
            integration="langchain",
            observed=observed,
            resources=resources,
            compatibility=compatibility,
            borrowed_resources=ManagedResources(checkpointer, store),
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
        checkpointed=checkpointer is not None
        or resources is not None
        or config.checkpoints is not None,
    )


def observe(
    model: ModelInput,
    tools: Sequence[BaseTool | Callable[..., Any]] = (),
    *,
    openeight: OpenEightConfig | OpenEightConfigDict,
    middleware: Sequence[AgentMiddleware] = (),
    system_prompt: str | SystemMessage | None = None,
    state_schema: type[Any] | None = None,
    context_schema: type[Any] | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    store: BaseStore | None = None,
    response_format: ResponseFormatInput = None,
    name: str | None = None,
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
        state_schema=state_schema,
        context_schema=context_schema,
        checkpointer=checkpointer,
        store=store,
        response_format=response_format,
        name=name,
    )
