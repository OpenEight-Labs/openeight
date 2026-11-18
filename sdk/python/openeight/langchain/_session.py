"""Async application ownership shared by the qualified framework constructors."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import replace
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from langchain_core.globals import get_debug
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.config import get_config_list
from langgraph.types import Command, StreamMode

from openeight import ConfigurationError, Governance
from openeight._managed_types import (
    CatalogReadiness,
    CatalogSnapshot,
    CatalogStatus,
    ManagedResources,
)
from openeight._time import utc_timestamp
from openeight._validation import validate_timeout
from openeight.delivery import DeliveryStatus
from openeight.errors import TransportError
from openeight.integration import AgentIntegration
from openeight.lifecycle import ExecutionOutcome

from ._owner import InstrumentedAgent
from ._runtime import AgentInvocation, invocation_context
from ._session_catalog import catalog_snapshot, catalog_status

if TYPE_CHECKING:
    from openeight.deepagents.durable import DurableThreadStatus
    from openeight.langchain.durable import DurableAgentResult

    from ._sync import ManagedStream

_ITEM_INDEX = ContextVar("openeight_batch_item", default=0)
_OUTCOMES = ContextVar("openeight_operation_outcomes", default=None)


def safe_diagnostics():
    if get_debug():
        raise ConfigurationError("unsafe_framework_diagnostics")


async def settle(task):
    """Repeated caller cancellation must not release borrowed resources early."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            break
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class _SessionOwner:
    def __init__(
        self,
        build,
        governance,
        *,
        prepare,
        select_model,
        required_features=(),
        runtime_models=None,
        checkpoints=None,
        integration: AgentIntegration,
        observed=False,
        saver=None,
        paths=((),),
        resources=None,
        compatibility=None,
        borrowed_resources=None,
        durable_deployment=None,
        durability=None,
    ):
        if not isinstance(governance, Governance):
            raise ConfigurationError("invalid_integration_configuration")
        self._pid = os.getpid()
        self._mode = None
        self._bridge = None
        self._compatibility_selection = None
        self._compatibility = compatibility
        self._required_features = frozenset(required_features)
        self._resource_factory = resources
        self._resources = borrowed_resources or ManagedResources()
        if resources is not None and (
            not callable(resources)
            or self._resources.checkpointer is not None
            or self._resources.store is not None
        ):
            raise ConfigurationError("integration_resource_conflict")
        self._resource_context = None
        self._resource_entered = False
        self._resource_exit = None
        self._operation_tasks = set()
        self._operation_drain = None
        self._cleanup_error = False
        self._cleanup_status = None
        self._prepare = prepare
        self._select_model = select_model
        self._runtime_models = runtime_models
        self._checkpoints = checkpoints
        self._prepared_models = None
        self._integration = integration
        self._durable_deployment = durable_deployment
        self._durability = durability
        self._local_deployment = None
        self._local_runtime = None
        if durable_deployment is not None and (
            integration != "deepagents" or governance.durable is None or observed
        ):
            raise ConfigurationError("invalid_integration_configuration")
        self._build, self._governance = build, governance
        if durability is not None:
            from openeight.deepagents.durable import LocalDurability

            if (
                integration != "deepagents"
                or not isinstance(durability, LocalDurability)
                or observed
                or governance.durable is not None
                or governance.approval is not None
                or durable_deployment is not None
                or governance.sources
                or governance.mcp_tools is not None
                or governance.discovery is not None
            ):
                raise ConfigurationError("invalid_integration_configuration")
            if (
                resources is not None
                or self._resources.checkpointer is not None
                or self._resources.store is not None
            ):
                raise ConfigurationError("integration_resource_conflict")
            self._resource_factory = self._open_local_resources
        self._observed, self._saver = observed, saver
        self._owner = None
        self._loop = None
        self._entered = False
        self._closed = False
        self._coordinator = None
        self._stop_discovery = None
        self._close_sources = None
        self._paths = paths

    @asynccontextmanager
    async def _open_local_resources(self):
        from openeight.deepagents._local_durable._runtime import (
            compose_local_governance,
            open_local_durability,
        )

        async with open_local_durability(self._durability) as runtime:
            self._local_runtime = runtime
            self._governance = compose_local_governance(
                self._governance, self._durability, runtime
            )
            yield runtime.resources

    def _coordinator_build(self, remotes):
        graph, bindings, controls, _ = self._assemble_generation(remotes)
        return graph, bindings, list(controls)

    def _assemble_generation(self, remotes):
        from ._variants import STATIC_KEY, VariantCatalog, assemble_variant_generation

        names = (
            tuple(self._prepared_models)
            if self._runtime_models is not None
            else (None,)
        )
        generation = assemble_variant_generation(
            self._build,
            scoped=self._saver,
            variant_names=names,
            resources=self._resources,
            governance=self._governance,
            remotes=remotes,
            generation=self._coordinator.current.number + 1
            if self._coordinator and self._coordinator.current
            else 1,
        )
        self._generation_builds = generation.builds
        for name, built in generation.builds.items():
            if self._runtime_models is None:
                continue
            measured = built.composition.get("runtime_model_configuration")
            if measured is None:
                descriptor = built.composition.get("effective_configuration", {})
                roots = [
                    node for node in descriptor.get("nodes", ()) if not node["path"]
                ]
                measured = roots[0]["runtime_variant"] if len(roots) == 1 else None
            expected = self._prepared_models[name]
            if (
                measured is None
                or measured.get("name") != name
                or measured.get("configuration_sha256") != expected.configuration_sha256
            ):
                raise ConfigurationError("invalid_integration_configuration")
        if self._runtime_models is None or self._governance.durable is not None:
            if len(generation.builds) != 1:
                raise ConfigurationError("invalid_integration_configuration")
            built = generation.builds[
                STATIC_KEY if self._runtime_models is None else names[0]
            ]
            return built.graph, built.bindings, built.controls, built.composition
        catalog = VariantCatalog(generation)
        controls = tuple(
            control
            for built in generation.builds.values()
            for control in built.controls
        )
        return generation, catalog, controls, None

    def _check_process(self):
        if os.getpid() != self._pid:
            raise ConfigurationError("instrumentation_process_changed")

    def _check_mode(self, mode):
        self._check_process()
        if self._mode is None:
            raise ConfigurationError("integration_not_entered")
        if self._mode != mode:
            raise ConfigurationError("integration_mode_mismatch")

    def _select_mode(self, mode):
        self._check_process()
        if self._mode is not None:
            if self._mode != mode:
                raise ConfigurationError("integration_mode_mismatch")
            raise ConfigurationError("invalid_integration_configuration")
        self._mode = mode

    def _select_features(self, *features):
        from openeight._framework import detect_framework, select_compatibility

        required = self._required_features | frozenset(features)
        installed = (
            self._compatibility_selection.installed
            if self._compatibility_selection
            else detect_framework(self._integration)
        )
        selection = select_compatibility(installed, required=required)
        if (
            self._compatibility_selection is not None
            and selection.qualification_id
            != self._compatibility_selection.qualification_id
        ):
            raise ConfigurationError("unsupported_framework_feature")
        self._compatibility_selection = selection
        # Stream is validated per call; it does not permanently modify owner features.
        return selection

    def _check_loop(self):
        self._check_process()
        try:
            valid = self._loop is asyncio.get_running_loop()
        except RuntimeError:
            valid = False
        if not valid:
            raise ConfigurationError("integration_loop_changed")

    def _require(self):
        if self._owner is None:
            raise ConfigurationError("integration_not_entered")
        self._check_loop()
        self._owner._ensure_open()
        return self._owner

    async def _open_resources(self):
        if self._checkpoints is not None and (
            self._governance.durable is not None
            or self._durability is not None
            or not self._governance.endpoint.startswith("https://")
        ):
            raise ConfigurationError("unsupported_execution_method")
        if self._resource_factory is not None:
            self._resource_context = self._resource_factory()
            self._resources = await self._resource_context.__aenter__()
            self._resource_entered = True
            if not isinstance(self._resources, ManagedResources):
                raise ConfigurationError("invalid_integration_configuration")
        if self._resources.checkpointer is not None and not self._governance.durable:
            self._select_features("checkpoint")
            self._required_features |= {"checkpoint"}
        if self._governance.sources and self._resources.checkpointer is not None:
            raise ConfigurationError("unsupported_checkpointer")
        if (
            self._governance.durable is None
            and self._resources.checkpointer is not None
        ):
            if self._checkpoints is not None:
                from ._checkpoint_claim import ScopedCheckpointAccess

                self._saver = ScopedCheckpointAccess(self._resources.checkpointer)
            else:
                from ._checkpoint import ScopedSaver

                self._saver = ScopedSaver(self._resources.checkpointer)
        if self._checkpoints is not None and self._saver is None:
            raise ConfigurationError("unsupported_checkpointer")

    def _attach_owner(self, graph, bindings, controls, composition):
        governance = self._governance
        if self._durability is not None:
            from openeight.deepagents._admission import LocalDeploymentContext

            self._local_deployment = LocalDeploymentContext(
                self._durability.revision, composition["configuration_sha256"]
            )
        from openeight._content_capture import validate_content_policy

        owner = InstrumentedAgent(
            graph,
            integration=self._integration,
            agent_name=governance.agent_name,
            endpoint=governance.endpoint,
            credential=governance.credential,
            delivery=governance.delivery,
            control=None if self._observed else controls[0],
            _bindings=bindings,
            _compatibility=self._compatibility,
            _compatibility_selection=self._compatibility_selection,
            _composition=composition,
            _durable_deployment=self._durable_deployment,
            _local_deployment=self._local_deployment,
            _content_policy=validate_content_policy(
                governance.capture_content, governance.redact_content
            ),
        )
        for control in controls:
            control.owner = owner
        self._owner = owner
        owner.coordinator = self._coordinator
        if self._coordinator:
            self._coordinator.attach(owner)

    async def _validate_entry(self):
        governance, owner = self._governance, self._owner
        if self._integration == "deepagents" and governance.durable is not None:
            from openeight.deepagents._admission import (
                _current_runtime,
                _validate_durable_admission,
            )

            async def validate_entry():
                store = governance.durable.store
                with store.acquire_deployment():
                    if (
                        self._local_runtime is not None
                        and self._local_runtime.store._deployment_id is None
                    ):
                        return
                    deployment = store.deployment()
                    _current_runtime(owner, deployment)
                    if deployment.manifest_json is not None:
                        await _validate_durable_admission(owner)

            await self._run_operation(validate_entry)
        elif (
            self._integration == "langchain"
            and governance.durable is not None
            and governance.durable.deployment_binding is not None
        ):
            from ._deployment import ownership, validate_current

            with ownership(governance.durable.deployment_binding):
                if validate_current(owner, allow_unbound=True) is not None:
                    owner._defer_catalog = False
                    owner.catalog_delivery.request_refresh()

    async def _enter(self):
        if self._entered or self._closed:
            raise ConfigurationError("invalid_integration_configuration")
        safe_diagnostics()
        self._entered = True
        self._loop = asyncio.get_running_loop()
        features = (
            {"durable"}
            if self._governance.durable or self._durability is not None
            else {"ordinary"}
        )
        if self._durability is not None:
            features.add("local_durable")
        if self._mode == "sync":
            features.add("sync")
        if self._governance.sources:
            features.add("mcp")
        if self._resources.checkpointer is not None and not self._governance.durable:
            features.add("checkpoint")
        self._required_features |= frozenset(features)
        self._select_features()
        try:
            await self._open_resources()
            self._prepared_models = await self._prepare(
                resources=self._resources, governance=self._governance
            )
            if self._runtime_models is not None and tuple(
                self._prepared_models
            ) != tuple(item.name for item in self._runtime_models.variants):
                raise ConfigurationError("invalid_integration_configuration")
            if self._governance.sources:
                from openeight.mcp._coordinator import Coordinator

                self._coordinator = Coordinator(
                    self._governance, self._paths, self._coordinator_build
                )
            composition = None
            if self._coordinator:
                generation = await self._coordinator.refresh(initial=True)
                graph, bindings, controls = (
                    generation.graph,
                    generation.bindings,
                    generation.controls,
                )
            else:
                graph, bindings, controls, composition = self._assemble_generation({})
            self._attach_owner(graph, bindings, controls, composition)
            await self._validate_entry()
        except BaseException as entry_error:
            if self._owner is not None:
                try:
                    await self._exit(None, None, None)
                except BaseException:
                    self._cleanup_error = True
                    raise entry_error from ConfigurationError(
                        "integration_resource_cleanup_failed"
                    )
                raise

            if self._coordinator:
                await settle(asyncio.create_task(self._coordinator.close()))
            self._closed = True
            if self._resource_entered:
                try:
                    await settle(self._start_resource_exit())
                except BaseException:
                    self._cleanup_error = True
                    self._cleanup_status = DeliveryStatus(
                        closed=False,
                        last_error_code="integration_resource_cleanup_failed",
                        discovery_diagnostic=None,
                    )
                    raise entry_error from ConfigurationError(
                        "integration_resource_cleanup_failed"
                    )
            raise
        return self

    async def _exit(self, *exc):
        async def drain():
            while not (await self._close()).closed:
                await asyncio.sleep(0.01)

        await settle(asyncio.create_task(drain()))

    def _new_thread(self):
        self._require()
        if self._saver is None or self._checkpoints is not None:
            raise ConfigurationError("unsupported_execution_method")
        return self._saver.new_thread()

    def _release_thread(self, thread_id: str) -> None:
        """Relinquish an idle checkpoint thread without deleting its saved rows."""
        self._require()
        if self._saver is None or self._checkpoints is not None:
            raise ConfigurationError("unsupported_execution_method")
        self._saver.release_thread(thread_id)

    def _config(self, config):
        if config is None:
            config = {}
        if not isinstance(config, Mapping) or set(config) - {
            "tags",
            "metadata",
            "callbacks",
            "recursion_limit",
            "max_concurrency",
            "configurable",
        }:
            raise ConfigurationError("unsupported_execution_method")
        result = dict(config)
        selectors = result.get("configurable", {})
        metadata = result.get("metadata", {})
        if (
            not isinstance(selectors, Mapping)
            or not isinstance(metadata, Mapping)
            or any(
                str(key).startswith(("openeight", "__")) for key in metadata
            )
        ):
            raise ConfigurationError("invalid_integration_configuration")
        if self._runtime_models is not None:
            from openeight._framework._runtime_models import CHECKPOINT_KEYS

            model_keys = set(selectors) - CHECKPOINT_KEYS
            metadata = {
                key: item for key, item in metadata.items() if key not in model_keys
            }
            selectors = {
                key: item for key, item in selectors.items() if key in CHECKPOINT_KEYS
            }
        allowed_selectors = (
            {"thread_id", "checkpoint_ns", "checkpoint_id", "checkpoint_map"}
            if self._checkpoints is not None
            else {"thread_id"}
            if self._saver
            else set()
        )
        if set(selectors) - allowed_selectors:
            raise ConfigurationError("unsupported_execution_method")
        result["configurable"] = dict(selectors)
        result["metadata"] = dict(metadata)
        return result

    async def _admit(self, value, config, operation):
        owner = self._require()
        safe_diagnostics()
        selected = None
        if self._runtime_models is not None:
            if config is not None and not isinstance(config, Mapping):
                raise ConfigurationError("invalid_integration_configuration")
            selected = self._select_model(config or {})
            if self._prepared_models.get(selected.variant_name) is not selected:
                raise ConfigurationError("invalid_integration_configuration")
        config = self._config(config)
        if self._coordinator:
            await self._coordinator.admission()
        owner.catalog.validate()
        thread = None
        if self._saver and self._checkpoints is None:
            thread = self._saver.threads.get(config["configurable"].get("thread_id"))
            if thread is None:
                raise ConfigurationError("checkpoint_thread_unowned")
            if thread["busy"]:
                raise ConfigurationError("checkpoint_thread_busy")
            if (
                selected is not None
                and thread["pending"]
                and thread.get("model_variant") != selected.variant_name
            ):
                raise ConfigurationError("checkpoint_resume_invalid")
        if self._checkpoints is not None:
            from ._checkpoint_claim import CURRENT_CHECKPOINT_CLAIM

            scope = CURRENT_CHECKPOINT_CLAIM.get()
            if scope is None:
                raise ConfigurationError("checkpoint_scope_invalid")
            thread = {
                "busy": True,
                "pending": scope.native_pending,
                "claim_scope": scope,
            }
        if isinstance(value, Command) and (
            thread is None
            or not thread["pending"]
            or value.resume is None
            or value.update is not None
            or value.goto
            or value.graph is not None
        ):
            raise ConfigurationError("checkpoint_resume_invalid")
        if thread:
            thread["busy"] = True
            if selected is not None:
                thread["model_variant"] = selected.variant_name
        invocation = None
        try:
            owner._ensure_open()
            with owner.lock:
                if len(owner.roots) >= 1000 or not owner.worker.reserve(512):
                    raise ConfigurationError("execution_busy")
                invocation = AgentInvocation(owner, operation)
                index = _ITEM_INDEX.get()
                outcomes = _OUTCOMES.get()
                if outcomes is not None:
                    while len(outcomes) <= index:
                        outcomes.append(
                            ExecutionOutcome(
                                item_index=len(outcomes),
                                run_id=None,
                                state="not_started",
                            )
                        )
                    outcomes[index] = ExecutionOutcome(
                        item_index=index, run_id=invocation.run_id, state="unsettled"
                    )
                if selected is None:
                    invocation.graph = owner._graph
                else:
                    from ._variants import VariantGeneration

                    if not isinstance(owner._graph, VariantGeneration):
                        raise ConfigurationError("invalid_integration_configuration")
                    built = owner._graph.builds[selected.variant_name]
                    invocation.graph, invocation.catalog = built.graph, built.bindings
                    invocation.generation_catalog = owner.catalog
                    invocation.variant_name = selected.variant_name
                owner.roots.add(invocation)
            if self._checkpoints is not None:
                from ._checkpoint_workflow import begin_execution

                await begin_execution(scope, invocation.run_id)
            elif thread is not None:
                await owner._run_client_call(
                    lambda: owner.client.get_tool_readiness([])
                )
            owner._ensure_open()
            invocation.start()
            if self._checkpoints is not None:
                scope.invocation = invocation
            return (
                invocation,
                owner._prepare_runnable_config(config, invocation),
                thread,
            )
        except BaseException:
            if invocation is not None:
                owner.release(invocation)
            if thread is not None:
                thread["busy"] = False
            raise

    async def _finish(self, invocation, thread):
        if thread is None or "claim_scope" not in thread:
            invocation.finish_execution()
        if thread is not None or self._mode == "sync":

            async def release():
                while invocation.handlers:
                    await asyncio.sleep(0.01)
                if thread is not None:
                    thread["busy"] = False

            await settle(asyncio.create_task(release()))

    async def _invoke(self, input, config=None, *, context=None):
        if self._checkpoints is not None:
            from ._checkpoint_task import invoke
            from ._checkpoint_workflow import claimed_execution

            async def execute():
                async with claimed_execution(self, input, config, context):
                    return await self._invoke_owned(input, config, context=context)

            return await invoke(execute)
        return await self._invoke_owned(input, config, context=context)

    async def _invoke_owned(self, input, config=None, *, context=None):
        from openeight._framework._structured_output import invoke_native

        invocation, config, thread = await self._admit(input, config, "invoke")
        state = "failed"
        try:
            with invocation_context(invocation):
                result = await invoke_native(
                    lambda: invocation.graph.ainvoke(input, config, context=context)
                )
            if thread is not None:
                snapshot = await invocation.graph.aget_state(config)
                thread["pending"] = bool(snapshot.interrupts)
            state = "completed"
            return result
        except asyncio.CancelledError:
            state = "unsettled" if invocation.handlers else "cancelled"
            raise
        finally:
            await self._finish(invocation, thread)
            index = _ITEM_INDEX.get()
            outcomes = _OUTCOMES.get()
            if outcomes is not None:
                outcomes[index] = ExecutionOutcome(
                    item_index=index, run_id=invocation.run_id, state=state
                )

    async def _stream(
        self, input, config=None, *, context=None, stream_mode="values", subgraphs=False
    ):
        if self._checkpoints is not None:
            from ._checkpoint_task import stream
            from ._checkpoint_workflow import claimed_execution

            async def execute():
                async with claimed_execution(self, input, config, context):
                    iterator = self._stream_owned(
                        input,
                        config,
                        context=context,
                        stream_mode=stream_mode,
                        subgraphs=subgraphs,
                    )
                    try:
                        async for item in iterator:
                            yield item
                    finally:
                        await iterator.aclose()

            iterator = stream(execute)
        else:
            iterator = self._stream_owned(
                input,
                config,
                context=context,
                stream_mode=stream_mode,
                subgraphs=subgraphs,
            )
        try:
            async for item in iterator:
                yield item
        finally:
            await iterator.aclose()

    async def _stream_owned(
        self, input, config=None, *, context=None, stream_mode="values", subgraphs=False
    ):
        from openeight._framework._structured_output import stream_native

        self._select_features("stream")
        if (
            stream_mode not in {"values", "updates", "messages"}
            or type(subgraphs) is not bool
        ):
            raise ConfigurationError("unsupported_execution_method")
        invocation, config, thread = await self._admit(input, config, "stream")
        iterator = None
        try:
            with invocation_context(invocation):
                iterator = stream_native(
                    invocation.graph.astream(
                        input,
                        config,
                        context=context,
                        stream_mode=stream_mode,
                        subgraphs=subgraphs,
                    )
                ).__aiter__()
            while True:
                try:
                    with invocation_context(invocation):
                        chunk = await anext(iterator)
                except StopAsyncIteration:
                    if thread is not None:
                        snapshot = await invocation.graph.aget_state(config)
                        thread["pending"] = bool(snapshot.interrupts)
                    break
                if thread is not None:
                    value = chunk[1] if subgraphs else chunk
                    if isinstance(value, dict) and "__interrupt__" in value:
                        thread["pending"] = bool(value["__interrupt__"])
                yield chunk
        finally:
            try:
                if iterator is not None:
                    with invocation_context(invocation):
                        await iterator.aclose()
            finally:
                await self._finish(invocation, thread)

    async def _batch(
        self, inputs, config=None, *, return_exceptions=False, context=None
    ):
        if not inputs:
            return []
        configs = get_config_list(config, len(inputs))
        # get_config_list/ensure_config introduce native defaults; strip only those defaults.
        for index, item in enumerate(configs):
            item.pop("run_name", None)
            original = config[index] if isinstance(config, list) else config
            if original is None or "recursion_limit" not in original:
                item.pop("recursion_limit", None)
        limit = configs[0].get("max_concurrency", len(inputs))
        if type(limit) is not int or limit < 1:
            raise ConfigurationError("invalid_integration_configuration")
        semaphore = asyncio.Semaphore(limit)

        outcomes = _OUTCOMES.get()
        if outcomes is not None:
            outcomes[:] = [
                ExecutionOutcome(item_index=index, run_id=None, state="not_started")
                for index in range(len(inputs))
            ]

        async def run(index, value, cfg):
            token = _ITEM_INDEX.set(index)
            try:
                async with semaphore:
                    return await self._invoke(value, cfg, context=context)
            finally:
                _ITEM_INDEX.reset(token)

        tasks = [
            asyncio.create_task(run(index, value, cfg))
            for index, (value, cfg) in enumerate(zip(inputs, configs))
        ]
        try:
            return await asyncio.gather(*tasks, return_exceptions=return_exceptions)
        finally:
            if self._mode == "sync" and any(not task.done() for task in tasks):
                # A first error must not release the sync slot ahead of its siblings.
                async def drain():
                    await asyncio.gather(*tasks, return_exceptions=True)

                await settle(asyncio.create_task(drain()))

    def _catalog_snapshot(self) -> CatalogSnapshot:
        return catalog_snapshot(self._require(), self._coordinator)

    def _catalog_status(self, *, settled=False) -> CatalogStatus:
        owner = self._owner if settled else self._require()
        return catalog_status(owner, self._coordinator)

    def _delivery_status(self):
        if self._owner is None:
            if self._cleanup_status is not None:
                return self._cleanup_status
            raise ConfigurationError("integration_not_entered")
        status = self._cleanup_status or self._owner.delivery_status()
        if self._coordinator and not self._closed:
            return replace(status, closed=False)
        return status

    async def _readiness(self, timeout=10.0):
        from ._bindings import CatalogReadiness

        owner = self._require()
        deadline = time.monotonic() + validate_timeout(timeout)
        if (
            self._integration == "langchain"
            and self._governance.durable is not None
            and self._governance.durable.deployment_binding is not None
        ):
            from ._deployment import ownership, validate_admission

            async def validate_langchain():
                with ownership(self._governance.durable.deployment_binding):
                    await validate_admission(owner, deadline=deadline)

            await self._run_operation(validate_langchain)
        if self._integration == "deepagents" and self._governance.durable is not None:
            from openeight.deepagents._admission import _validate_durable_admission

            async def read_bound():
                with owner.control.durable.store.acquire_deployment():
                    if self._local_runtime is not None:
                        self._local_runtime.validate_ready()
                    await _validate_durable_admission(owner, deadline=deadline)
                    return CatalogReadiness(
                        1, utc_timestamp(), owner._bound_readiness, ()
                    )

            return await self._run_operation(read_bound)
        snapshot = self._catalog_snapshot()
        versions = [item.tool_version.id for item in snapshot.results]
        result = await owner._run_client_call(
            lambda: owner.client.get_tool_readiness(versions),
            deadline=deadline,
        )
        published = {item.tool.name for item in snapshot.results}
        return CatalogReadiness(
            snapshot.generation,
            utc_timestamp(),
            result,
            tuple(fact.name for fact in snapshot.facts if fact.name not in published),
        )

    async def _refresh_tools(self, timeout=10.0):
        owner = self._require()
        deadline = validate_timeout(timeout)
        if (
            self._integration == "langchain"
            and self._governance.durable is not None
            and self._governance.durable.deployment_binding is not None
        ):
            await self._readiness(deadline)
        owner.catalog.validate()
        if self._integration == "deepagents" and self._governance.durable is not None:
            await self._readiness(deadline)
            return self._catalog_status()
        if self._coordinator:
            try:
                await asyncio.wait_for(self._coordinator.refresh(), deadline)
            except TimeoutError:
                for source in self._coordinator.sources.values():
                    source.invalidate()
                raise TransportError() from None
        else:
            owner.catalog_delivery.request_refresh()
        return self._catalog_status()

    async def _flush(self, timeout=10.0):
        return await self._require().aflush(timeout)

    async def _run_operation(self, operation):
        owner = self._require()
        with owner.lock:
            owner._ensure_open()
            task = asyncio.create_task(operation())
            self._operation_tasks.add(task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if not task.cancelled():
                task.exception()
            raise
        finally:
            self._operation_tasks.discard(task)

    async def _close(self, timeout=10.0):
        self._check_loop()
        if self._cleanup_error:
            raise ConfigurationError("integration_resource_cleanup_failed")
        deadline = time.monotonic() + validate_timeout(timeout)
        if self._owner is None:
            raise ConfigurationError("integration_not_entered")

        async def wait(task):
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), max(0.001, deadline - time.monotonic())
                )
                return True
            except TimeoutError:
                return False

        if self._operation_tasks:
            with self._owner.lock:
                self._owner.closed = True
            if self._operation_drain is None:
                operations = tuple(self._operation_tasks)

                async def drain_operations():
                    await asyncio.gather(*operations, return_exceptions=True)

                self._operation_drain = asyncio.create_task(drain_operations())
            if not await wait(self._operation_drain):
                return replace(
                    self._owner.delivery_status(),
                    closed=False,
                    last_error_code="execution_busy",
                )
        if self._coordinator:
            with self._owner.lock:
                self._owner.closed = True
            self._coordinator.stopped = True
            if self._stop_discovery is None:
                self._stop_discovery = asyncio.create_task(
                    self._coordinator.stop_refresh()
                )
            if not await wait(self._stop_discovery):
                return replace(
                    self._owner.delivery_status(),
                    closed=False,
                    last_error_code="execution_busy",
                )
        result = await self._owner.aclose(max(0.001, deadline - time.monotonic()))
        if result.closed:
            if self._coordinator:
                if self._close_sources is None:
                    self._close_sources = asyncio.create_task(self._coordinator.close())
                if not await wait(self._close_sources):
                    return replace(
                        result, closed=False, last_error_code="execution_busy"
                    )
            if self._resource_entered:
                task = self._start_resource_exit()
                try:
                    done = await wait(task)
                except asyncio.CancelledError:
                    if not task.done() or not task.cancelled():
                        raise
                    self._cleanup_error = True
                    self._cleanup_status = replace(
                        result,
                        closed=False,
                        last_error_code="integration_resource_cleanup_failed",
                    )
                    raise ConfigurationError(
                        "integration_resource_cleanup_failed"
                    ) from None
                except Exception:
                    self._cleanup_error = True
                    self._cleanup_status = replace(
                        result,
                        closed=False,
                        last_error_code="integration_resource_cleanup_failed",
                    )
                    raise ConfigurationError(
                        "integration_resource_cleanup_failed"
                    ) from None
                if not done:
                    self._cleanup_status = replace(result, closed=False)
                    return self._cleanup_status
            self._cleanup_status = None
            self._closed = True
            if self._saver and self._checkpoints is None:
                self._saver.threads.clear()
        return result

    def _start_resource_exit(self):
        if self._resource_exit is None:
            self._resource_exit = asyncio.create_task(
                self._resource_context.__aexit__(None, None, None)
            )
        return self._resource_exit

    def __enter__(self) -> Self:
        from ._sync import SyncBridge

        self._select_mode("sync")
        self._bridge = SyncBridge(self)
        return self._bridge.enter()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._check_mode("sync")
        return self._bridge.exit(exc_type, exc, traceback)

    async def __aenter__(self) -> Self:
        self._select_mode("async")
        return await self._enter()

    async def __aexit__(self, *exc: Any) -> None:
        self._check_mode("async")
        try:
            return await self._exit(*exc)
        except BaseException as cleanup:
            if len(exc) > 1 and exc[1] is not None:
                raise exc[1].with_traceback(exc[2]) from cleanup
            raise

    def _sync_call(self, operation, *, category="execution"):
        self._check_mode("sync")
        return self._bridge.call(operation, category=category)

    def invoke(
        self, input: Any, config: RunnableConfig | None = None, *, context: Any = None
    ) -> Any:
        return self._sync_call(lambda: self._invoke(input, config, context=context))

    async def ainvoke(
        self, input: Any, config: RunnableConfig | None = None, *, context: Any = None
    ) -> Any:
        self._check_mode("async")
        return await self._invoke(input, config, context=context)

    def batch(
        self,
        inputs: Sequence[Any],
        config: RunnableConfig | list[RunnableConfig] | None = None,
        *,
        return_exceptions: bool = False,
        context: Any = None,
    ) -> list[Any]:
        return self._sync_call(
            lambda: self._batch(
                inputs, config, return_exceptions=return_exceptions, context=context
            )
        )

    async def abatch(
        self,
        inputs: Sequence[Any],
        config: RunnableConfig | list[RunnableConfig] | None = None,
        *,
        return_exceptions: bool = False,
        context: Any = None,
    ) -> list[Any]:
        self._check_mode("async")
        return await self._batch(
            inputs, config, return_exceptions=return_exceptions, context=context
        )

    def stream(
        self,
        input: Any,
        config: RunnableConfig | None = None,
        *,
        context: Any = None,
        stream_mode: StreamMode | list[StreamMode] = "values",
        subgraphs: bool = False,
    ) -> ManagedStream:
        self._check_mode("sync")
        return self._bridge.iterate(
            lambda: self._stream(
                input,
                config,
                context=context,
                stream_mode=stream_mode,
                subgraphs=subgraphs,
            )
        )

    async def astream(
        self,
        input: Any,
        config: RunnableConfig | None = None,
        *,
        context: Any = None,
        stream_mode: StreamMode | list[StreamMode] = "values",
        subgraphs: bool = False,
    ) -> AsyncGenerator[Any, None]:
        self._check_mode("async")
        iterator = self._stream(
            input, config, context=context, stream_mode=stream_mode, subgraphs=subgraphs
        )
        try:
            async for item in iterator:
                yield item
        finally:
            await iterator.aclose()

    def readiness(self, timeout: float = 10.0) -> CatalogReadiness:
        return self._sync_call(lambda: self._readiness(timeout), category="status")

    async def areadiness(self, timeout: float = 10.0) -> CatalogReadiness:
        self._check_mode("async")
        return await self._readiness(timeout)

    def refresh_tools(self, timeout: float = 10.0) -> CatalogStatus:
        return self._sync_call(lambda: self._refresh_tools(timeout), category="status")

    async def arefresh_tools(self, timeout: float = 10.0) -> CatalogStatus:
        self._check_mode("async")
        return await self._refresh_tools(timeout)

    def flush(self, timeout: float = 10.0) -> DeliveryStatus:
        return self._sync_call(lambda: self._flush(timeout), category="status")

    async def aflush(self, timeout: float = 10.0) -> DeliveryStatus:
        self._check_mode("async")
        return await self._flush(timeout)

    def close(self, timeout: float = 10.0) -> DeliveryStatus:
        self._check_mode("sync")
        if self._bridge.stopped and self._closed and not self._cleanup_error:
            return self._delivery_status()
        return self._bridge.close(timeout)

    async def aclose(self, timeout: float = 10.0) -> DeliveryStatus:
        self._check_mode("async")
        return await self._close(timeout)

    def _helper(self, method, *args, category="status"):
        self._check_process()
        if self._mode == "sync":

            async def call():
                return method(*args)

            return self._sync_call(call, category=category)
        return method(*args)

    def catalog_snapshot(self) -> CatalogSnapshot:
        return self._helper(self._catalog_snapshot)

    def catalog_status(self) -> CatalogStatus:
        return self._helper(self._catalog_status)

    def delivery_status(self) -> DeliveryStatus:
        self._check_process()
        if self._bridge and self._bridge.stopped:
            return self._delivery_status()
        return self._helper(self._delivery_status)

    def new_thread(self) -> str:
        return self._helper(self._new_thread, category="execution")

    def release_thread(self, thread_id: str) -> None:
        return self._helper(self._release_thread, thread_id, category="execution")

    async def _deployment_lifecycle(self, *, bind):
        self._require()
        raise ConfigurationError("unsupported_execution_method")

    async def _bind_deployment(self):
        await self._deployment_lifecycle(bind=True)

    async def _validate_deployment(self):
        await self._deployment_lifecycle(bind=False)

    def bind_deployment(self) -> None:
        return self._sync_call(self._bind_deployment)

    async def abind_deployment(self) -> None:
        self._check_mode("async")
        return await self._bind_deployment()

    def validate_deployment(self) -> None:
        return self._sync_call(self._validate_deployment)

    async def avalidate_deployment(self) -> None:
        self._check_mode("async")
        return await self._validate_deployment()


class DurableAgent(_SessionOwner):
    async def _invoke(self, *args, **kwargs):
        raise ConfigurationError("unsupported_execution_method")

    async def _stream(self, *args, **kwargs):
        raise ConfigurationError("unsupported_execution_method")
        yield

    async def _deployment_lifecycle(self, *, bind):
        safe_diagnostics()
        owner = self._require()
        if (
            self._integration == "langchain"
            and owner.control.durable.deployment_binding is not None
        ):
            from ._deployment import ownership, validate_admission

            async def validate_langchain():
                with ownership(owner.control.durable.deployment_binding):
                    await validate_admission(owner, bind=bind)

            await self._run_operation(validate_langchain)
            return
        if self._integration != "deepagents":
            raise ConfigurationError("unsupported_execution_method")
        from openeight.deepagents._admission import _validate_durable_admission

        async def validate():
            with owner.control.durable.store.acquire_deployment():
                await _validate_durable_admission(owner, bind=bind)

        await self._run_operation(validate)

    async def _invoke_durable(self, input, *, thread_id, command_id):
        safe_diagnostics()
        owner = self._require()
        owner.catalog.validate()
        return await self._run_operation(
            lambda: owner.ainvoke_durable(
                input, thread_id=thread_id, command_id=command_id
            )
        )

    async def _reply(self, message, *, thread_id, command_id):
        safe_diagnostics()
        owner = self._require()
        owner.catalog.validate()
        return await self._run_operation(
            lambda: owner.areply(message, thread_id=thread_id, command_id=command_id)
        )

    async def _resume(self, *, thread_id, command_id):
        safe_diagnostics()
        owner = self._require()
        owner.catalog.validate()
        return await self._run_operation(
            lambda: owner.aresume(thread_id=thread_id, command_id=command_id)
        )

    async def _batch(self, *args, **kwargs):
        raise ConfigurationError("unsupported_execution_method")

    def invoke_durable(
        self, input: Any, *, thread_id: str, command_id: str
    ) -> DurableAgentResult:
        return self._sync_call(
            lambda: self._invoke_durable(
                input, thread_id=thread_id, command_id=command_id
            )
        )

    async def ainvoke_durable(
        self, input: Any, *, thread_id: str, command_id: str
    ) -> DurableAgentResult:
        self._check_mode("async")
        return await self._invoke_durable(
            input, thread_id=thread_id, command_id=command_id
        )

    def reply(
        self, message: str, *, thread_id: str, command_id: str
    ) -> DurableAgentResult:
        return self._sync_call(
            lambda: self._reply(message, thread_id=thread_id, command_id=command_id)
        )

    async def areply(
        self, message: str, *, thread_id: str, command_id: str
    ) -> DurableAgentResult:
        self._check_mode("async")
        return await self._reply(message, thread_id=thread_id, command_id=command_id)

    def resume(self, *, thread_id: str, command_id: str) -> DurableAgentResult:
        return self._sync_call(
            lambda: self._resume(thread_id=thread_id, command_id=command_id)
        )

    async def aresume(self, *, thread_id: str, command_id: str) -> DurableAgentResult:
        self._check_mode("async")
        return await self._resume(thread_id=thread_id, command_id=command_id)


class LocalDurableAgent(DurableAgent):
    """SDK-owned local command admission with the shared durable enforcement engine."""

    def _local(self):
        owner = self._require()
        if self._durability is None or self._local_runtime is None:
            raise ConfigurationError("unsupported_execution_method")
        return owner, self._local_runtime

    async def _initialize(self):
        safe_diagnostics()
        owner, runtime = self._local()
        from openeight.client import _validate_base_url
        from openeight.deepagents._admission import (
            _current_runtime,
            _validate_durable_admission,
        )
        from openeight.deepagents._deployment import canonical, credential_binding

        async def initialize():
            store = runtime.store
            with store.acquire_deployment():
                if store._deployment_id is None:
                    await owner._run_client_call(
                        lambda: owner.client.get_tool_readiness([])
                    )
                    identity = canonical(
                        dict(
                            kind="credential_bound",
                            endpoint=_validate_base_url(owner.binding[1]),
                            agent_name=owner.binding[0],
                            organization_id=None,
                            workspace_id=None,
                            agent_id=None,
                            api_key_id=None,
                        )
                    )
                    store.initialize_enrollment(
                        identity, credential_binding(owner.binding[2])
                    )
                _current_runtime(owner, store.deployment())
                await _validate_durable_admission(owner, bind=True)
                runtime.publish_ready()

        await self._run_operation(initialize)

    async def _deployment_lifecycle(self, *, bind):
        if bind:
            raise ConfigurationError("unsupported_execution_method")
        owner, runtime = self._local()
        from openeight.deepagents._admission import _validate_durable_admission

        async def validate():
            with runtime.store.acquire_deployment():
                runtime.validate_ready()
                await _validate_durable_admission(owner)

        await self._run_operation(validate)

    async def _admit_and_execute(self, kind, value, *, thread_id, command_id):
        safe_diagnostics()
        owner, runtime = self._local()
        from openeight.deepagents._admission import _validate_durable_admission
        from openeight.deepagents._local_durable._store import command_request

        from ._durable_execution import (
            _execute_admitted,
            _message,
            terminal_structured_result,
        )

        message = _message(kind, value)
        command_request(
            kind=kind, command_id=command_id, thread_id=thread_id, message=message
        )

        async def execute():
            store = runtime.store
            with store.acquire_deployment():
                runtime.validate_ready()
                await _validate_durable_admission(owner)
                admission = store.admit(
                    kind=kind,
                    command_id=command_id,
                    thread_id=thread_id,
                    message=message,
                )
                with store.acquire(admission.thread_id):
                    existing = store.command_result(admission.thread_id, command_id)
                    if existing is not None:
                        result = existing
                    else:
                        result = await _execute_admitted(
                            owner, kind, message, admission.thread_id, command_id
                        )
                    return await terminal_structured_result(
                        owner, admission.thread_id, command_id, result
                    )

        return await self._run_operation(execute)

    async def _invoke_durable(self, *args, **kwargs):
        raise ConfigurationError("unsupported_execution_method")

    async def _status(self, *, thread_id):
        from uuid import UUID

        from openeight.deepagents._admission import _validate_durable_admission

        safe_diagnostics()
        owner, runtime = self._local()
        try:
            if type(thread_id) is not str or str(UUID(thread_id)) != thread_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ConfigurationError("invalid_input") from None

        async def inspect():
            with runtime.store.acquire_deployment():
                runtime.validate_ready()
                await _validate_durable_admission(owner)
                with runtime.store.acquire(thread_id):
                    return runtime.store.thread_status(thread_id)

        return await self._run_operation(inspect)

    def status(self, *, thread_id: str) -> DurableThreadStatus:
        """Read current local thread state without admitting or executing a command."""
        return self._sync_call(lambda: self._status(thread_id=thread_id))

    async def astatus(self, *, thread_id: str) -> DurableThreadStatus:
        """Read current local thread state using this owner's async execution style."""
        self._check_mode("async")
        return await self._status(thread_id=thread_id)

    async def _start(self, input, *, command_id):
        return await self._admit_and_execute(
            "start", input, thread_id=None, command_id=command_id
        )

    async def _reply(self, message, *, thread_id, command_id):
        return await self._admit_and_execute(
            "reply", message, thread_id=thread_id, command_id=command_id
        )

    async def _resume(self, *, thread_id, command_id):
        return await self._admit_and_execute(
            "resume", None, thread_id=thread_id, command_id=command_id
        )

    def initialize(self) -> None:
        return self._sync_call(self._initialize)

    async def ainitialize(self) -> None:
        self._check_mode("async")
        return await self._initialize()

    def start(self, input: Any, *, command_id: str) -> DurableAgentResult:
        return self._sync_call(lambda: self._start(input, command_id=command_id))

    async def astart(self, input: Any, *, command_id: str) -> DurableAgentResult:
        self._check_mode("async")
        return await self._start(input, command_id=command_id)
