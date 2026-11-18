"""Measure the qualified effective Deep graph and verify retained native instances."""

import hashlib
from functools import partial
from inspect import getclosurevars, isfunction, ismethod

from langchain.agents.middleware import AgentMiddleware, TodoListMiddleware
from langchain.agents.middleware.todo import _awrite_todos, _write_todos
from langchain_core.messages import SystemMessage
from langchain_core.messages.utils import count_tokens_approximately

from openeight._framework._dispatch import dispatch_signature as dispatch_signature
from openeight._framework._measurement import (
    MeasuredConfiguration,
    freeze,
    payload_hash,
    plain,
    qualified,
)
from openeight.errors import ConfigurationError
from openeight.langchain._catalog import tool_schema

from .._deployment import canonical


def invalid():
    raise ConfigurationError("unsupported_graph_shape")


def compiled_middleware(graph):
    objects, seen = {}, set()

    def visit(value):
        if id(value) in seen:
            return
        seen.add(id(value))
        if ismethod(value):
            if isinstance(value.__self__, AgentMiddleware):
                objects[id(value.__self__)] = value.__self__
            return
        if isfunction(value):
            for item in getclosurevars(value).nonlocals.values():
                visit(item)
        elif type(value) in (tuple, list):
            for item in value:
                visit(item)

    for node in graph.nodes.values():
        bound = node.bound
        for method in ("func", "afunc", "_wrap_tool_call", "_awrap_tool_call"):
            visit(getattr(bound, method, None))
    return objects


def _fields(item, names, *, extra=()):
    if set(vars(item)) != set(names) | set(extra):
        invalid()
    return {name: getattr(item, name) for name in names}


def middleware_payload(item, *, model, backend, control, child_paths, ordinary=False):
    from deepagents.middleware.filesystem import (
        FilesystemMiddleware,
        FilesystemPermission,
    )
    from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
    from deepagents.middleware.subagents import SubAgentMiddleware
    from deepagents.middleware.summarization import _DeepAgentsSummarizationMiddleware
    from deepagents.middleware.unsupported_content import UnsupportedContentMiddleware
    from langchain.agents.middleware.summarization import SummarizationMiddleware
    from langchain_anthropic.middleware.prompt_caching import (
        AnthropicPromptCachingMiddleware,
    )

    from .._middleware import GovernanceBoundary, ModelSelectionGuard

    if type(item) is ModelSelectionGuard:
        if set(vars(item)) != {"controller"} or item.controller is not control:
            invalid()
        if control._managed_model is not model:
            invalid()
        return {"kind": "prepared-model", "revision_bound": True}

    if type(item) is FilesystemMiddleware:
        names = (
            "_blobs_prefix",
            "_conversation_history_prefix",
            "_custom_system_prompt",
            "_custom_tool_descriptions",
            "_enabled_tools",
            "_grep_max_count",
            "_human_message_token_limit_before_evict",
            "_large_tool_results_prefix",
            "_max_execute_timeout",
            "_offload_binary_content",
            "_permissions",
            "_tool_token_limit_before_evict",
        )
        result = _fields(
            item,
            names,
            extra=("backend", "state_schema", "_glob_executor", "_glob_slots", "tools"),
        )
        if item.backend is not backend:
            invalid()
        result["_enabled_tools"] = (
            None if item._enabled_tools is None else sorted(item._enabled_tools)
        )
        rules = []
        for rule in item._permissions:
            if (
                type(rule) is not FilesystemPermission
                or set(vars(rule)) != {"operations", "paths", "mode"}
                or rule.mode
                not in (
                    {"allow", "deny", "interrupt"} if ordinary else {"allow", "deny"}
                )
            ):
                invalid()
            rules.append(
                {key: getattr(rule, key) for key in ("operations", "paths", "mode")}
            )
        result["_permissions"] = rules
        return result
    if type(item) is _DeepAgentsSummarizationMiddleware:
        names = (
            "_truncate_args_trigger",
            "_truncate_args_keep",
            "_max_arg_length",
            "_truncation_text",
            "_history_path_prefix",
            "_large_tool_results_prefix",
            "_media_prefix",
            "_counter_accepts_tools",
        )
        result = _fields(item, names, extra=("_lc_helper", "_backend"))
        helper = item._lc_helper
        if (
            item._backend is not backend
            or type(helper) is not SummarizationMiddleware
            or helper.model is not model
        ):
            invalid()
        expected_helper_keys = {
            "model",
            "_summary_model",
            "trigger",
            "_trigger_clauses",
            "_trigger_conditions",
            "keep",
            "token_counter",
            "_partial_token_counter",
            "summary_prompt",
            "trim_tokens_to_summarize",
        }
        if set(vars(helper)) != expected_helper_keys:
            invalid()
        baseline = SummarizationMiddleware(
            model=model,
            trigger=helper.trigger,
            keep=helper.keep,
            summary_prompt=helper.summary_prompt,
            trim_tokens_to_summarize=helper.trim_tokens_to_summarize,
        )
        for field in ("token_counter", "_partial_token_counter"):
            current, expected = getattr(helper, field), getattr(baseline, field)
            if (
                type(current) is not partial
                or current.func is not count_tokens_approximately
                or current.func is not expected.func
                or current.args != expected.args
                or current.keywords != expected.keywords
            ):
                invalid()
        for field in ("_trigger_clauses", "_trigger_conditions"):
            if getattr(helper, field) != getattr(baseline, field):
                invalid()
        retry, expected_retry = helper._summary_model, baseline._summary_model
        if (
            type(retry) is not type(expected_retry)
            or retry.bound is not model
            or vars(retry) != vars(expected_retry)
        ):
            invalid()
        result.update(
            {
                key: getattr(helper, key)
                for key in (
                    "trigger",
                    "keep",
                    "summary_prompt",
                    "trim_tokens_to_summarize",
                )
            }
        )
        result["model_max_input_tokens"] = helper._get_profile_limits()
        return result
    if type(item) is SubAgentMiddleware:
        _fields(
            item,
            (
                "system_prompt",
                "_task_description",
                "_private_state_keys",
                "_backend",
                "_subagents",
                "_state_schema",
                "subagent_names",
                "tools",
            ),
        )
        if item._backend is not backend or len(item.tools) != 1:
            invalid()
        return {
            "system_prompt": item.system_prompt,
            "_task_description": item._task_description,
            "_private_state_keys": sorted(item._private_state_keys),
            "task_description": item.tools[0].description,
            "children": [list(path) for path in child_paths],
        }
    if type(item) is TodoListMiddleware:
        if (
            len(item.tools) != 1
            or item.tools[0].func is not _write_todos
            or item.tools[0].coroutine is not _awrite_todos
        ):
            invalid()
        _fields(item, ("system_prompt", "tool_description", "tools"))
        if item.tool_description != item.tools[0].description:
            invalid()
        return {
            "system_prompt": item.system_prompt,
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "schema": tool_schema(tool),
                }
                for tool in item.tools
            ],
        }
    if type(item) is GovernanceBoundary:
        baseline = GovernanceBoundary(control, backend=backend)
        if (
            item.controller is not control
            or item._backend is not backend
            or item.sources
            or item._approvals.interrupt_on
        ):
            invalid()
        if set(vars(item)) != set(vars(baseline)) or any(
            getattr(item, name) != getattr(baseline, name)
            for name in ("source_labels", "system_prompt_template")
        ):
            invalid()
        if (
            item._skill_tool_resolver.__code__
            is not baseline._skill_tool_resolver.__code__
            or vars(item._approvals) != vars(baseline._approvals)
        ):
            invalid()
        return {
            "path": list(control.child_path),
            "sources": [],
            "interrupt_on": {},
            "observed": False,
            "control_kind": qualified(control),
        }
    if type(item) is AnthropicPromptCachingMiddleware:
        return _fields(
            item, ("type", "ttl", "min_messages_to_cache", "unsupported_model_behavior")
        )
    if type(item) in (PatchToolCallsMiddleware, UnsupportedContentMiddleware):
        return _fields(item, ())
    invalid()


def _node(
    graph,
    *,
    path,
    resolved,
    stack,
    bindings,
    controls,
    backend,
    topology,
    response_digest=None,
    variant_name=None,
):
    node = resolved.nodes[path]
    fields = getclosurevars(graph.nodes["model"].bound.func).nonlocals
    asynchronous = getclosurevars(graph.nodes["model"].bound.afunc).nonlocals
    if (
        fields["model"] is not node.model
        or asynchronous["model"] is not node.model
        or fields["system_message"] != asynchronous["system_message"]
    ):
        invalid()
    prompt = fields["system_message"]
    if prompt is None:
        prompt = SystemMessage(content="")
    if type(prompt) is not SystemMessage:
        invalid()
    message = prompt.model_dump(mode="json")
    if set(message) != {
        "content",
        "additional_kwargs",
        "response_metadata",
        "type",
        "name",
        "id",
    }:
        invalid()
    live = compiled_middleware(graph)
    if {id(item) for item in stack} != set(live):
        invalid()
    children = [tuple(value) for value in topology["delegates_to"]]
    entries = []
    for item in stack:
        entries.append(
            {
                "kind": qualified(item),
                "configuration_sha256": payload_hash(
                    middleware_payload(
                        item,
                        model=node.model,
                        backend=backend,
                        control=controls[path],
                        child_paths=children,
                    )
                ),
            }
        )
    tools = []
    visible = {tool.name: tool for tool in fields["default_tools"]}
    for (tool_path, name), binding in sorted(
        bindings.native.items(), key=lambda pair: pair[0][1].encode()
    ):
        if tool_path != path:
            continue
        if name not in visible or visible[name] is not binding.tool:
            invalid()
        tools.append(
            {
                "native_name": name,
                "catalog_name": binding.facts.name,
                "input_schema_sha256": hashlib.sha256(binding.schema_bytes).hexdigest(),
                "executable_description_sha256": payload_hash(binding.tool.description),
                "model_visible_description_sha256": payload_hash(
                    visible[name].description
                ),
            }
        )
    task = next(
        (item for item in stack if type(item).__name__ == "SubAgentMiddleware"), None
    )
    return {
        "path": list(path),
        "source": node.source,
        "model_class": qualified(node.model),
        "model_spec_sha256": None
        if node.original_model_string is None
        else payload_hash(node.original_model_string),
        "system_message_sha256": payload_hash(message),
        "middleware": entries,
        "tools": tools,
        "delegates_to": topology["delegates_to"],
        "state_schema_kind": qualified(graph.builder.state_schema),
        "private_state_keys": sorted(task._private_state_keys) if task else [],
        "default_config_sha256": payload_hash(graph.config or {}),
        "runtime_variant": None
        if variant_name is None
        else {"name": variant_name, "configuration_sha256": node.configuration_sha256},
        "response_format_sha256": response_digest,
    }


def capture_configuration(
    *,
    graphs,
    resolved,
    stacks,
    bindings,
    controls,
    backend,
    composition,
    response_spec=None,
    variant_name=None,
):
    descriptor = {
        "format_version": 2,
        "nodes": [],
        "catalog_naming": composition["catalog_naming"],
        "backend": composition["backend"],
        "saver": composition["saver"],
    }
    for topology in composition["topology"]:
        path = tuple(topology["path"])
        descriptor["nodes"].append(
            _node(
                graphs[path],
                path=path,
                resolved=resolved,
                stack=stacks[path],
                bindings=bindings,
                controls=controls,
                backend=backend,
                topology=topology,
                response_digest=response_spec.schema_sha256
                if response_spec is not None and not path
                else None,
                variant_name=variant_name,
            )
        )
    digest = hashlib.sha256(canonical(descriptor).encode()).hexdigest()
    return MeasuredConfiguration(freeze(descriptor), digest)


def measure_configuration(build):
    descriptor = build.composition.get("effective_configuration")
    if descriptor is None:
        invalid()
    value = plain(descriptor)
    digest = hashlib.sha256(canonical(value).encode()).hexdigest()
    if digest != build.composition.get("effective_configuration_sha256"):
        invalid()
    if build.verify_configuration is None:
        invalid()
    live = build.verify_configuration()
    if live.sha256 != digest or live.descriptor != descriptor:
        invalid()
    return MeasuredConfiguration(freeze(value), digest)
