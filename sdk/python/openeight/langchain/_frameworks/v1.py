"""Qualified LangChain native graph and checkpoint interpretation."""

from types import MappingProxyType

from langchain.agents import create_agent as native_create

from openeight import ConfigurationError
from openeight._framework._types import ManagedBuild
from openeight.langchain._bindings import AdapterBindings
from openeight.langchain._compat import inspect_agent
from openeight.langchain._qualified import validate_managed_versions


class LangChainV1:
    def pause_for_approval(self, decision_id):
        from langgraph.types import interrupt

        return interrupt({"decision_id": decision_id})

    def validate_durable_saver(self, graph):
        saver = graph.checkpointer
        if (
            type(saver).__name__ != "AsyncSqliteSaver"
            or type(saver).__module__ != "langgraph.checkpoint.sqlite.aio"
            or getattr(getattr(saver, "serde", None), "pickle_fallback", False)
        ):
            raise ConfigurationError("unsupported_checkpointer")

    def execution_observations(self, *, thread_id, snapshot=None):
        return None

    def recovery_namespaces(self, snapshot):
        return frozenset()

    def build(self, request):
        self._response_spec = request.response_spec
        model, tools = request.resolved, request.tools
        from langchain_core.language_models import BaseChatModel

        from openeight._framework._runtime_models import ResolvedInvocationModel

        selected = model if isinstance(model, ResolvedInvocationModel) else None
        model = selected.model if selected is not None else model
        if not isinstance(model, BaseChatModel):
            raise ConfigurationError("invalid_integration_configuration")
        middleware = request.options["middleware"]
        system_prompt = request.options["system_prompt"]
        state_schema = request.options["state_schema"]
        context_schema = request.options["context_schema"]
        response_format = request.options["response_format"]
        name = request.options["name"]
        governance = request.options["governance"]
        remotes = request.options["remotes"]
        validate_managed_versions()
        control = request.controls[()]
        control._managed_model = model
        graph = native_create(
            model=model,
            tools=[*tools, *(b.tool for b in remotes.get((), ()))],
            middleware=[*middleware, control],
            system_prompt=system_prompt,
            state_schema=state_schema,
            context_schema=context_schema,
            checkpointer=request.checkpointer,
            store=request.store,
            response_format=response_format,
            name=name,
            debug=False,
        )
        capabilities = inspect_agent(graph, control)
        if "tools" in graph.nodes and capabilities.discovery_completeness != "complete":
            raise ConfigurationError("unsupported_graph_shape")
        bindings = AdapterBindings(
            {(): capabilities.tools},
            remotes={id(b.tool): b for bindings in remotes.values() for b in bindings},
            max_tools=governance.discovery.max_tools if governance.discovery else 200,
            max_bytes=governance.discovery.max_catalog_bytes
            if governance.discovery
            else 1048576,
        )
        composition = {}
        if selected is not None:
            if request.variant_name != selected.variant_name:
                raise ConfigurationError("invalid_integration_configuration")
            composition["runtime_model_configuration"] = MappingProxyType(
                {
                    "name": selected.variant_name,
                    "configuration_sha256": selected.configuration_sha256,
                }
            )
        verify = None
        if request.options.get("checkpoints") or request.options.get(
            "deployment_binding"
        ):
            from openeight._framework._dispatch import dispatch_signature
            from openeight.langchain._checkpoint_measurement import (
                capture_nodes,
                native_hitl_nodes,
            )

            signature = dispatch_signature(graph)
            from openeight._framework._structured_output import response_configuration

            response_signature = response_configuration(graph)

            def verify():
                if dispatch_signature(graph) != signature:
                    raise ConfigurationError("unsupported_graph_shape")
                if response_configuration(graph) != response_signature:
                    raise ConfigurationError("unsupported_graph_shape")
                inspect_agent(graph, control)
                bindings.validate()
                return capture_nodes(
                    graphs={(): graph},
                    stacks={(): (*middleware, control)},
                    bindings=bindings,
                    models={
                        (): (
                            model,
                            "explicit",
                            request.model if isinstance(request.model, str) else None,
                            selected.configuration_sha256 if selected else None,
                        )
                    },
                    controls={(): control},
                    response_spec=request.response_spec,
                    variant_name=request.variant_name,
                )

            composition["checkpoint_nodes"] = verify()
            composition["checkpoint_hitl_nodes"] = native_hitl_nodes({(): middleware})
        self._verify_configuration = verify
        return ManagedBuild(
            graph, bindings, (control,), MappingProxyType(composition), verify
        )

    def validate(self, selection, *, required):
        from openeight._framework import select_compatibility

        selected = select_compatibility(selection.installed, required=required)
        if selected.implementation_id != selection.implementation_id:
            raise ConfigurationError("unsupported_framework_version")

    def inspect(self, build):
        capabilities = inspect_agent(build.graph, build.controls[0])
        if (
            "tools" in build.graph.nodes
            and capabilities.discovery_completeness != "complete"
        ):
            raise ConfigurationError("unsupported_graph_shape")
        build.bindings.validate()

    async def read_checkpoint(self, graph, config, *, checkpoint_id=None):
        from openeight._framework._types import FrameworkSnapshot

        if checkpoint_id is not None:
            config = {
                **config,
                "configurable": {
                    **config["configurable"],
                    "checkpoint_id": checkpoint_id,
                },
            }
        snapshot = await graph.aget_state(config)
        identifier = (
            snapshot.config.get("configurable", {}).get("checkpoint_id")
            if snapshot.config
            else None
        )
        if identifier is not None and (type(identifier) is not str or not identifier):
            raise ConfigurationError("durable_checkpoint_conflict")
        messages = (
            snapshot.values.get("messages", []) if type(snapshot.values) is dict else []
        )
        ids = [message.id for message in messages]
        if any(type(i) is not str or not i for i in ids) or len(set(ids)) != len(ids):
            raise ConfigurationError("durable_checkpoint_conflict")
        return FrameworkSnapshot(
            identifier, bool(snapshot.next), frozenset(ids), snapshot
        )

    def visible_output(self, snapshot, *, command_id):
        native = snapshot.native
        messages = (
            native.values.get("messages", []) if type(native.values) is dict else []
        )
        positions = [i for i, m in enumerate(messages) if m.id == command_id]
        if len(positions) != 1:
            raise ConfigurationError("durable_checkpoint_conflict")
        result = []
        for message in messages[positions[0] + 1 :]:
            if message.type == "human":
                break
            if message.type == "ai" and not message.tool_calls and message.text:
                result.append(str(message.text))
        return tuple(result)

    def output_since(self, snapshot, *, before):
        messages = snapshot.native.values.get("messages", [])
        return tuple(
            str(m.text)
            for m in messages
            if m.id not in before.message_ids
            and m.type == "ai"
            and not m.tool_calls
            and m.text
        )

    def message_input(self, messages):
        from langchain_core.messages import HumanMessage

        return {
            "messages": [
                HumanMessage(content=body, id=identifier)
                for identifier, body in messages
            ]
        }

    async def invoke_durable(self, graph, native_input, config):
        from openeight._framework._structured_output import invoke_native

        await invoke_native(lambda: graph.ainvoke(native_input, config))

    def structured_response(self, snapshot):
        from openeight._framework._structured_output import project_structured_response

        values = snapshot.native.values
        if (
            self._response_spec is None
            or type(values) is not dict
            or "structured_response" not in values
        ):
            return False, None
        return True, project_structured_response(
            self._response_spec, values["structured_response"]
        )

    def tool_result(self, message):
        from langchain_core.messages import ToolMessage

        from openeight._framework._types import NativeToolResult

        if (
            not isinstance(message, ToolMessage)
            or type(message.content) is not str
            or message.status not in {"success", "error"}
        ):
            raise ConfigurationError("invalid_tool_result")
        return NativeToolResult(
            message.tool_call_id, message.name, message.content, message.status
        )

    def tool_message(self, result):
        from langchain_core.messages import ToolMessage

        return ToolMessage(
            content=result.content,
            status=result.status,
            tool_call_id=result.tool_call_id,
            name=result.tool_name,
        )

    def native_identity(self, request, *, invocation, child_path):
        raise ConfigurationError("unsupported_framework_feature")

    def is_interrupt(self, error):
        from langgraph.errors import GraphInterrupt

        return isinstance(error, GraphInterrupt)

    def suspend_reconciliation(self):
        raise ConfigurationError("durable_reconciliation_required")

    def pending_delegations(self, snapshot, *, invocation):
        return frozenset()

    def resume_input(self, snapshot, *, invocation, continuations):
        from langgraph.types import Command

        interrupts = [
            item for task in snapshot.native.tasks for item in task.interrupts
        ]
        if not interrupts:
            return None
        known = {a.decision_id for a in invocation.store.attempts(invocation.thread_id)}
        values = {}
        for item in interrupts:
            if (
                type(item.id) is not str
                or not item.id
                or item.id in values
                or type(item.value) is not dict
                or set(item.value) != {"decision_id"}
                or item.value["decision_id"] not in known
            ):
                raise ConfigurationError("invalid_resume_correlation")
            values[item.id] = item.value
        return Command(resume=values)

    async def recover_generated(self, invocation, attempt, continuation, snapshot):
        raise ConfigurationError("unsupported_framework_feature")

    def generated_recovery_kind(self, binding):
        return None
