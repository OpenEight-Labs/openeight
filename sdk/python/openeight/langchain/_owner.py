"""Shared invocation ownership for synchronous and asynchronous execution."""

import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from types import MappingProxyType

from langchain_core.runnables import Runnable
from langchain_core.runnables.config import ensure_config, get_config_list
from langgraph.types import Command

from openeight import Client, ConfigurationError, DeliveryConfig, TransportError
from openeight._content_capture import FAILURE_REASONS, validate_content_policy
from openeight._delivery import DeliveryWorker
from openeight._validation import validate_timeout
from openeight.client import _ADAPTER_DEADLINE
from openeight.integration import AgentIntegration

from ._callbacks import ObservationCallbacks
from ._catalog import BoundToolCatalog, CatalogDelivery
from ._runtime import AgentInvocation, invocation_context


class InstrumentedAgent(Runnable):
    """Observe native create_agent execution and own its delivery lifecycle."""

    def __init__(
        self,
        graph,
        *,
        agent_name,
        endpoint,
        credential,
        integration: AgentIntegration,
        delivery=None,
        control=None,
        _bindings=None,
        _content_policy=None,
        _compatibility=None,
        _compatibility_selection=None,
        _durable_deployment=None,
        _composition=None,
        _local_deployment=None,
    ):
        from openeight._framework import (
            detect_framework,
            load_compatibility,
            select_compatibility,
        )
        from openeight._framework._measurement import plain

        self._integration = integration
        self._durable_deployment = _durable_deployment
        self._local_deployment = _local_deployment
        self._composition = plain(_composition) if _composition is not None else None
        self._bound_catalog = None
        self._bound_readiness = None
        if (
            integration == "deepagents"
            and control is not None
            and control.durable is not None
        ):
            from openeight.deepagents._admission import LocalDeploymentContext
            from openeight.deepagents.durable import (
                DeepAgentsDeploymentConfig,
                validate_store,
            )

            if (
                (_durable_deployment is None) == (_local_deployment is None)
                or _durable_deployment is not None
                and not isinstance(_durable_deployment, DeepAgentsDeploymentConfig)
                or _local_deployment is not None
                and not isinstance(_local_deployment, LocalDeploymentContext)
                or self._composition is None
            ):
                raise ConfigurationError("invalid_integration_configuration")
            validate_store(control.durable.store)
        self._defer_catalog = (
            control is not None
            and control.durable is not None
            and (
                integration == "deepagents"
                or getattr(control.durable, "deployment_binding", None) is not None
            )
        )
        self._compatibility_selection = (
            _compatibility_selection
            or select_compatibility(
                detect_framework(integration),
                required=frozenset(
                    {"durable"} if control and control.durable else {"ordinary"}
                ),
            )
        )
        self._compatibility = _compatibility or load_compatibility(
            self._compatibility_selection
        )
        self.content_policy = _content_policy or validate_content_policy("none", None)
        self.content_capture_failures = dict.fromkeys(FAILURE_REASONS, 0)
        self.catalog = (
            _bindings if _bindings is not None else BoundToolCatalog(graph, control)
        )
        self.fresh_contracts = _bindings is not None
        self.delivery = delivery if delivery is not None else DeliveryConfig()
        if not isinstance(self.delivery, DeliveryConfig):
            raise ConfigurationError("invalid_delivery_configuration")
        if control is not None and control.durable is not None:
            from ._durable_execution import validate_durable_graph
            from ._injected import hidden_action_key

            validate_durable_graph(graph, integration=integration)
            for entry in self.catalog.entries.values():
                hidden_action_key(entry.tool)
        self.client = Client(
            endpoint, api_key=credential, agent_name=agent_name, integration=integration
        )
        self.client._adapter_owned_send = True
        self._graph, self.control = graph, control
        self.binding = (agent_name, endpoint, credential, self.delivery)
        self.worker = DeliveryWorker(
            self.client,
            self.delivery,
            self.catalog.completeness,
            self.catalog.diagnostic,
        )
        self.lock = threading.RLock()
        self.roots = set()
        self.closed = False
        self.pid = os.getpid()
        self.executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="openeight-http"
        )
        self.executor_slots = threading.BoundedSemaphore(1000)
        self.attempts = 0
        self.http_calls = 0
        self.resources_closed = False
        self._transport_close = None
        self.catalog_delivery = CatalogDelivery(self)
        try:
            if not self._defer_catalog:
                self.catalog_delivery.request_refresh()
        except BaseException:
            self.worker.close(1.0)
            self.executor.shutdown(wait=False, cancel_futures=True)
            self.client.close()
            raise

    def _ensure_process(self):
        if os.getpid() != self.pid:
            raise ConfigurationError("instrumentation_process_changed")

    def _ensure_open(self):
        self._ensure_process()
        if self.closed:
            raise ConfigurationError("integration_closed")

    def _admit_invocation(self, value, operation):
        """Reserve Run ownership and initialize catalog reporting before execution."""
        self._ensure_open()
        if self.control is not None and self.control.durable is not None:
            raise ConfigurationError("unsupported_execution_method")
        if isinstance(value, Command):
            raise ConfigurationError("unsupported_execution_method")
        with self.lock:
            self._ensure_open()
            if len(self.roots) >= 1000 or not self.worker.reserve(512):
                if self.control is not None:
                    raise ConfigurationError("execution_busy")
                return None
            invocation = AgentInvocation(self, operation)
            self.roots.add(invocation)
        try:
            invocation.start()
        except BaseException:
            self.release(invocation)
            raise
        return invocation

    async def ainvoke_durable(self, input, *, thread_id, command_id):
        from ._durable_execution import execute_durable

        return await execute_durable(self, "start", input, thread_id, command_id)

    async def areply(self, message, *, thread_id, command_id):
        from ._durable_execution import execute_durable

        return await execute_durable(self, "reply", message, thread_id, command_id)

    async def aresume(self, *, thread_id, command_id):
        from ._durable_execution import execute_durable

        return await execute_durable(self, "resume", None, thread_id, command_id)

    def release(self, invocation):
        with self.lock:
            self.roots.discard(invocation)
        self.worker.release(invocation.root_charge)
        if getattr(self, "coordinator", None) is not None:
            self.coordinator.release()

    def _prepare_runnable_config(self, config, invocation):
        """Copy caller configuration and attach only this invocation’s SDK callbacks."""
        result = dict(ensure_config(config))
        if config is None or "recursion_limit" not in config:
            result.pop("recursion_limit", None)
        for field in ("metadata", "configurable"):
            if field in result:
                result[field] = dict(result[field])
        if "tags" in result:
            result["tags"] = list(result["tags"])
        callbacks = result.get("callbacks")
        if callbacks is None or isinstance(callbacks, (list, tuple)):
            existing = list(callbacks or ())
            result["callbacks"] = [
                item for item in existing if not isinstance(item, ObservationCallbacks)
            ] + ([ObservationCallbacks(invocation)] if invocation else [])
        else:
            callbacks = callbacks.copy()
            for handler in list(callbacks.handlers):
                if isinstance(handler, ObservationCallbacks):
                    callbacks.remove_handler(handler)
            if invocation:
                callbacks.add_handler(
                    ObservationCallbacks(
                        invocation, external_parent_run_id=callbacks.parent_run_id
                    ),
                    inherit=True,
                )
            result["callbacks"] = callbacks
        return result

    async def _run_client_call(self, function, *, deadline=None, _closing=False):
        """Run a bounded client call off the event loop; queue wait consumes its deadline."""
        deadline = deadline if deadline is not None else time.monotonic() + 30.0
        if not self.executor_slots.acquire(blocking=False):
            raise ConfigurationError("execution_busy")
        context = copy_context()

        def bounded():
            if time.monotonic() >= deadline:
                raise TransportError() from None
            token = _ADAPTER_DEADLINE.set(deadline)
            try:
                return function()
            finally:
                _ADAPTER_DEADLINE.reset(token)

        try:
            with self.lock:
                if not _closing:
                    self._ensure_open()
                    self.http_calls += 1
        except BaseException:
            self.executor_slots.release()
            raise

        def release_slot(_=None):
            with self.lock:
                if not _closing:
                    self.http_calls -= 1
            self.executor_slots.release()

        try:
            future = self.executor.submit(context.run, bounded)
        except BaseException:
            release_slot()
            raise
        future.add_done_callback(release_slot)
        pending = asyncio.wrap_future(future)

        async def drain_started_call():
            cancelled = False
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    cancelled = True
                    continue
                except BaseException:
                    break
            # Consume failures even when the request settled between awaits.
            if pending.done() and not pending.cancelled():
                pending.exception()
            return cancelled

        try:
            return await asyncio.wait_for(
                asyncio.shield(pending), max(0, deadline - time.monotonic())
            )
        except TimeoutError:
            if not future.cancel() and await drain_started_call():
                raise asyncio.CancelledError
            raise TransportError() from None
        except asyncio.CancelledError:
            if future.cancel():
                raise
            # Drain a started credential-bearing call before its frame is released.
            await drain_started_call()
            raise

    def invoke(self, input, config=None, **kwargs):
        invocation = self._admit_invocation(input, "invoke")
        try:
            execution_config = self._prepare_runnable_config(config, invocation)
            with invocation_context(invocation):
                return self._graph.invoke(input, execution_config, **kwargs)
        finally:
            if invocation:
                invocation.finish_execution()

    async def _admit_invocation_async(self, value, operation):
        """Keep enforcement registration owned until started client calls settle."""
        if self.control is None:
            return self._admit_invocation(value, operation)
        admitted = []

        def begin():
            invocation = self._admit_invocation(value, operation)
            admitted.append(invocation)
            return invocation

        try:
            return await self._run_client_call(begin)
        except BaseException:
            if admitted and admitted[0] is not None:
                admitted[0].finish_execution()
            raise

    async def ainvoke(self, input, config=None, **kwargs):
        invocation = await self._admit_invocation_async(input, "invoke")
        try:
            execution_config = self._prepare_runnable_config(config, invocation)
            with invocation_context(invocation):
                return await self._graph.ainvoke(input, execution_config, **kwargs)
        finally:
            if invocation:
                invocation.finish_execution()

    def stream(self, input, config=None, **kwargs):
        if kwargs.get("stream_mode", "values") not in ("values", "updates", "messages"):
            raise ConfigurationError("unsupported_execution_method")
        invocation = self._admit_invocation(input, "stream")
        iterator = None
        try:
            execution_config = self._prepare_runnable_config(config, invocation)
            with invocation_context(invocation):
                iterator = iter(self._graph.stream(input, execution_config, **kwargs))
            while True:
                try:
                    with invocation_context(invocation):
                        chunk = next(iterator)
                except StopIteration:
                    break
                yield chunk
        finally:
            try:
                with invocation_context(invocation):
                    if iterator is not None:
                        iterator.close()
            finally:
                if invocation:
                    invocation.finish_execution()

    async def astream(self, input, config=None, **kwargs):
        if kwargs.get("stream_mode", "values") not in ("values", "updates", "messages"):
            raise ConfigurationError("unsupported_execution_method")
        invocation = await self._admit_invocation_async(input, "stream")
        iterator = None
        try:
            execution_config = self._prepare_runnable_config(config, invocation)
            with invocation_context(invocation):
                iterator = self._graph.astream(
                    input, execution_config, **kwargs
                ).__aiter__()
            while True:
                try:
                    with invocation_context(invocation):
                        chunk = await anext(iterator)
                except StopAsyncIteration:
                    break
                yield chunk
        finally:
            try:
                with invocation_context(invocation):
                    if iterator is not None:
                        await iterator.aclose()
            finally:
                if invocation:
                    invocation.finish_execution()

    def batch(self, inputs, config=None, *, return_exceptions=False, **kwargs):
        if not inputs:
            return []
        configs = get_config_list(config, len(inputs))

        def run(pair):
            try:
                return self.invoke(pair[0], pair[1], **kwargs)
            except Exception as error:
                if return_exceptions:
                    return error
                raise

        with ThreadPoolExecutor(
            max_workers=configs[0].get("max_concurrency")
        ) as executor:
            futures = [
                executor.submit(copy_context().run, run, pair)
                for pair in zip(inputs, configs)
            ]
            return [future.result() for future in futures]

    async def abatch(self, inputs, config=None, *, return_exceptions=False, **kwargs):
        if not inputs:
            return []
        configs = get_config_list(config, len(inputs))
        semaphore = asyncio.Semaphore(configs[0].get("max_concurrency") or len(inputs))

        async def run(value, config):
            async with semaphore:
                try:
                    return await self.ainvoke(value, config, **kwargs)
                except Exception as error:
                    if return_exceptions:
                        return error
                    raise

        return await asyncio.gather(
            *(run(value, cfg) for value, cfg in zip(inputs, configs))
        )

    def record_content_capture_failure(self, reason):
        if reason not in FAILURE_REASONS:
            raise ValueError("invalid_content_capture_reason")
        with self.lock:
            self.content_capture_failures[reason] += 1

    def delivery_status(self):
        """Return delivery counts and the actual resource-closure state."""
        from dataclasses import replace

        self._ensure_process()
        with self.lock:
            return replace(
                self.worker.status(),
                closed=self.resources_closed,
                content_capture_failures=MappingProxyType(
                    dict(self.content_capture_failures)
                ),
                **self.catalog_delivery.diagnostics(),
            )

    def flush(self, timeout=10.0):
        """Flush queued delivery without waiting for a running invocation to finish."""
        self._ensure_process()
        self.worker.flush(timeout)
        return self.delivery_status()

    async def aflush(self, timeout=10.0):
        """Flush asynchronously, returning delivery status on timeout or saturation."""
        self._ensure_process()
        deadline = time.monotonic() + validate_timeout(timeout)
        try:
            return await self._run_client_call(
                lambda: self.flush(max(0.001, deadline - time.monotonic())),
                deadline=deadline,
            )
        except (TransportError, ConfigurationError) as error:
            if (
                isinstance(error, ConfigurationError)
                and error.reason != "execution_busy"
            ):
                raise
            return self._delivery_error_status(
                "execution_busy"
                if isinstance(error, ConfigurationError)
                else "flush_timeout"
            )

    def _delivery_error_status(self, code):
        from dataclasses import replace

        with self.worker.condition:
            self.worker.last_error = code
        # Background delivery may report another error before this snapshot.
        return replace(self.delivery_status(), last_error_code=code)

    def close(self, timeout=10.0):
        """Seal admission and drain owned execution and delivery within one timeout."""
        self._ensure_process()
        deadline = time.monotonic() + validate_timeout(timeout)
        with self.lock:
            self.closed = True
        while True:
            with self.lock:
                busy = self.http_calls or any(
                    root.handlers or not root.root_ended for root in self.roots
                )
            if not busy:
                break
            if time.monotonic() >= deadline:
                return self._delivery_error_status("execution_busy")
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        with self.lock:
            roots = tuple(self.roots)
        for root in roots:
            if root.root_ended:
                root.expire_callbacks()
        status = self.worker.close(max(0.001, deadline - time.monotonic()))
        if not status.closed:
            return self.delivery_status()
        with self.lock:
            if self._transport_close is None:
                self._transport_close = self.executor.submit(self.client.close)
            future = self._transport_close
        try:
            future.result(timeout=max(0, deadline - time.monotonic()))
        except TimeoutError:
            return self._delivery_error_status("flush_timeout")
        except BaseException:
            with self.worker.condition:
                self.worker.last_error = "delivery_failed"
        self.resources_closed = True
        self.catalog_delivery.release()
        self.executor.shutdown(wait=False, cancel_futures=True)
        return self.delivery_status()

    async def aclose(self, timeout=10.0):
        """Close asynchronously; busy execution retains resources for a later close."""
        self._ensure_process()
        if self.resources_closed:
            return self.delivery_status()
        deadline = time.monotonic() + validate_timeout(timeout)
        with self.lock:
            self.closed = True
        try:
            return await self._run_client_call(
                lambda: self.close(max(0.001, deadline - time.monotonic())),
                deadline=deadline,
                _closing=True,
            )
        except (TransportError, ConfigurationError) as error:
            if (
                isinstance(error, ConfigurationError)
                and error.reason != "execution_busy"
            ):
                raise
            with self.lock:
                busy = isinstance(error, ConfigurationError) or any(
                    root.handlers or not root.root_ended for root in self.roots
                )
            return self._delivery_error_status(
                "execution_busy" if busy else "flush_timeout"
            )

    def __enter__(self):
        self._ensure_open()
        return self

    async def __aenter__(self):
        return self.__enter__()

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.close()
        except Exception:
            if exc is None:
                raise

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            await self.aclose()
        except Exception:
            if exc is None:
                raise

    async def astream_events(self, *args, **kwargs):
        raise ConfigurationError("unsupported_execution_method")
        yield

    async def astream_log(self, *args, **kwargs):
        raise ConfigurationError("unsupported_execution_method")
        yield

    def transform(self, *args, **kwargs):
        raise ConfigurationError("unsupported_execution_method")
        yield

    async def atransform(self, *args, **kwargs):
        raise ConfigurationError("unsupported_execution_method")
        yield

    def batch_as_completed(self, *args, **kwargs):
        raise ConfigurationError("unsupported_execution_method")
        yield

    async def abatch_as_completed(self, *args, **kwargs):
        raise ConfigurationError("unsupported_execution_method")
        yield
