"""Qualified static Deep Agents construction and topology interpretation."""

from types import MappingProxyType

from openeight import ConfigurationError
from openeight.langchain._frameworks.v1 import LangChainV1


class DeepAgentsV1(LangChainV1):
    def suspend_reconciliation(self):
        from langgraph.errors import GraphInterrupt

        # Keep the exact unfinished Send pending; no approval or result is created.
        raise GraphInterrupt(())

    def validate_durable_saver(self, graph):
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        saver = graph.checkpointer
        if (
            type(saver) is not AsyncSqliteSaver
            or type(getattr(saver, "serde", None)) is not JsonPlusSerializer
            or saver.serde.pickle_fallback is not False
        ):
            raise ConfigurationError("unsupported_checkpointer")

    def execution_observations(self, *, thread_id, snapshot=None):
        from ._identity import NativeObservations

        if snapshot is not None:
            return snapshot.native.observations
        return NativeObservations(paths=self._graphs, thread_id=thread_id)

    def recovery_namespaces(self, snapshot):
        return frozenset(task.namespace for task in snapshot.native.tasks)

    def build(self, request):
        from dataclasses import replace

        from ._assembly import assemble_deep
        from ._inspection import native_graphs
        from ._resolution import ResolvedTopology

        if not isinstance(request.resolved, ResolvedTopology):
            raise ConfigurationError("invalid_integration_configuration")
        backend = request.options["backend"]
        if backend is None:
            from deepagents.backends import StateBackend

            backend = StateBackend()
        if request.options["governance"].durable is not None:
            from deepagents.backends import StateBackend

            from ._generated import CapturedStateBackend

            if backend is not None and type(backend) not in {
                StateBackend,
                CapturedStateBackend,
            }:
                raise ConfigurationError("unsupported_graph_shape")
            backend = CapturedStateBackend()
        self._backend = backend
        request = replace(
            request, options=MappingProxyType({**request.options, "backend": backend})
        )
        built = assemble_deep(request, request.resolved)
        self._response_spec = request.response_spec
        self._graphs = MappingProxyType(native_graphs(built.graph, request.controls))
        self._bindings = built.bindings
        self._verify_configuration = built.verify_configuration
        return built

    def measure_configuration(self):
        if self._verify_configuration is None:
            raise ConfigurationError("durable_deployment_mismatch")
        return self._verify_configuration()

    def inspect(self, build):
        # Use the captured root and child graphs; do not rediscover mutable topology.
        build.bindings.validate()
        if "effective_configuration" in build.composition:
            from ._measurement import measure_configuration

            measure_configuration(build)
        elif "checkpoint_nodes" in build.composition:
            if (
                build.verify_configuration is None
                or build.verify_configuration() != build.composition["checkpoint_nodes"]
            ):
                raise ConfigurationError("unsupported_graph_shape")

    async def invoke_durable(self, graph, native_input, config):
        # Persist the task's input checkpoint before admitting its generated effects.
        # Middleware steps must not exhaust the native guard before the shared
        # 16-model-call budget. The captured static graphs bound each cycle.
        steps = 17 * max(len(item.nodes) for item in self._graphs.values())
        from openeight._framework._structured_output import invoke_native

        await invoke_native(
            lambda: graph.ainvoke(
                native_input, {**config, "recursion_limit": steps}, durability="sync"
            )
        )

    def structured_response(self, snapshot):
        from dataclasses import replace

        return super().structured_response(
            replace(snapshot, native=snapshot.native.root)
        )

    async def read_checkpoint(self, graph, config, *, checkpoint_id=None):
        from ._checkpoints import read_deep_checkpoint

        return await read_deep_checkpoint(
            self, graph, config, checkpoint_id=checkpoint_id
        )

    def visible_output(self, snapshot, *, command_id):
        from dataclasses import replace

        return super().visible_output(
            replace(snapshot, native=snapshot.native.root), command_id=command_id
        )

    def output_since(self, snapshot, *, before):
        from dataclasses import replace

        return super().output_since(
            replace(snapshot, native=snapshot.native.root), before=before
        )

    def native_identity(self, request, *, invocation, child_path):
        from langgraph.config import get_config

        return invocation.native_observations.observe(
            get_config(), request.tool_call, child_path=child_path
        )

    async def capture_generated(
        self, request, handler, *, invocation, attempt, continuation
    ):
        from openeight._framework._types import CapturedGeneratedResult
        from openeight.deepagents.durable import GeneratedStateReceipt

        from ._generated import capture_native_state

        if (
            attempt.deployment_id != invocation.deployment_id
            or attempt.thread_id != invocation.thread_id
            or attempt.decision_id != continuation.decision_id
            or request.tool_call["id"] != continuation.native_tool_call_id
        ):
            raise ConfigurationError("durable_attempt_conflict")
        capture = await capture_native_state(
            request, handler, compatibility=self, invocation=invocation
        )
        receipt = GeneratedStateReceipt(
            attempt.deployment_id,
            attempt.thread_id,
            attempt.decision_id,
            1,
            capture.input_checkpoint_id,
            capture.update_json,
        )
        return CapturedGeneratedResult(capture.result, receipt, capture.native_result)

    async def inspect_generated_replay(
        self, snapshot, *, invocation, attempt, continuation, receipt
    ):
        from ._replay import inspect_replay

        return await inspect_replay(
            self,
            snapshot,
            invocation=invocation,
            attempt=attempt,
            continuation=continuation,
            receipt=receipt,
        )

    def generated_result(self, result, *, invocation, continuation, receipt, replay):
        from langgraph.config import get_config

        from ._generated import CapturedNativeState, restore_native_state

        if replay.disposition not in {"replayable", "already_committed"}:
            raise ConfigurationError("durable_reconciliation_required")
        config = get_config()
        observation = invocation.native_observations.validate_runtime(config)
        selectors = config["configurable"]
        enclosing = (
            observation.identity.ancestor_namespaces[-1]
            if observation.identity.ancestor_namespaces
            else ""
        )
        if (
            observation.identity.checkpoint_namespace
            != continuation.checkpoint_namespace
            or observation.native_call_id != continuation.native_tool_call_id
            or selectors["checkpoint_map"].get(enclosing) != receipt.input_checkpoint_id
            or receipt.deployment_id != invocation.deployment_id
            or receipt.thread_id != invocation.thread_id
            or receipt.decision_id != continuation.decision_id
        ):
            raise ConfigurationError("durable_attempt_conflict")
        # A native parent resume can re-enter a completed sibling Send. Reuse
        # its proven output only while still executing the exact input checkpoint.
        captured = CapturedNativeState(
            result, receipt.input_checkpoint_id, receipt.update_json, None
        )
        return restore_native_state(captured, compatibility=self, backend=self._backend)

    def tool_output_routes(self, child_path):
        graph = self._graphs.get(child_path)
        if graph is None:
            raise ConfigurationError("durable_reconciliation_required")
        return frozenset(
            "branch:to:" + edge.target
            for edge in graph.get_graph().edges
            if edge.source == "tools" and edge.target != "__end__"
        )

    async def recover_generated(self, invocation, attempt, continuation, snapshot):
        from ._recovery import recover_generated

        return await recover_generated(
            self, invocation, attempt, continuation, snapshot
        )

    def pending_delegations(self, snapshot, *, invocation):
        from ._pending import pending_delegations

        return pending_delegations(snapshot, invocation=invocation)

    def resume_input(self, snapshot, *, invocation, continuations):
        from ._resume import resume_input

        return resume_input(
            snapshot, invocation=invocation, decision_ids=invocation.resume_decision_ids
        )

    def generated_recovery_kind(self, binding):
        from ._generated_tools import generated_kind

        return generated_kind(binding, backend=self._backend, graphs=self._graphs)
