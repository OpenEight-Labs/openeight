"""Bounded catalog generations and process-local MCP dispatch permits."""

import asyncio
import json
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass

from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool

from openeight._json import thaw_json
from openeight.errors import APIError, ConfigurationError, ProtocolError, TransportError

from ._source import Source
from .configuration import DiscoveryConfig, MCPSource, MCPTools

CURRENT_PERMIT = ContextVar("openeight_mcp_permit", default=None)


@dataclass
class Permit:
    remote: object
    invocation: object
    path: tuple
    observed: bool
    call: bytes
    session: object
    used: bool = False


class RemoteBinding:
    def __init__(self, source, remote_name, fact, raw):
        self.source, self.remote_name = source, remote_name
        self.fact, self.raw, self.epoch = fact, raw, source.epoch
        self.revoked = False
        self.tool = StructuredTool(
            name=fact.name,
            description=fact.description,
            args_schema=thaw_json(fact.input_schema),
            coroutine=self._execute,
        )

    def validate(self):
        if self.revoked:
            raise ConfigurationError("tool_binding_changed")
        self.source.validate(self.epoch)

    async def _execute(self, **args):
        permit = CURRENT_PERMIT.get()
        if permit is None or permit.remote is not self or permit.used:
            raise ConfigurationError("integration_not_entered")
        call = json.loads(permit.call)
        if call["args"] != args:
            raise ConfigurationError("tool_binding_changed")
        binding = permit.invocation.catalog.resolve(permit.path, self.fact.name)
        if binding is None or binding.remote is not self:
            raise ConfigurationError("tool_binding_changed")
        # Invalidation after this already-granted permit cannot undo admission.
        permit.used = True
        result = await self.source.call(self.remote_name, args, permit.session)
        return ToolMessage(
            content=json.dumps(result, ensure_ascii=False),
            tool_call_id=call["id"],
            name=self.fact.name,
            status="error" if result.get("isError") else "success",
        )


@dataclass
class Generation:
    number: int
    graph: object
    bindings: object
    controls: tuple


class Coordinator:
    def __init__(self, governance, paths, build):
        self.config = governance.discovery or DiscoveryConfig()
        if not isinstance(self.config, DiscoveryConfig):
            raise ConfigurationError("invalid_integration_configuration")
        sources = governance.sources
        if any(not isinstance(source, MCPSource) for source in sources) or len(
            {s.source_id for s in sources}
        ) != len(sources):
            raise ConfigurationError("invalid_integration_configuration")
        if len(sources) > self.config.max_tools:
            raise ConfigurationError("tool_discovery_limit_exceeded")
        self.changed = asyncio.Event()
        self.sources = {
            s.source_id: Source(s, self.config, self.changed) for s in sources
        }
        self.selections = governance.mcp_tools
        if self.selections is None:
            self.selections = {
                (): tuple(MCPTools(source_id) for source_id in self.sources)
            }
        if set(self.selections) - set(paths):
            raise ConfigurationError("invalid_integration_configuration")
        for selectors in self.selections.values():
            if any(
                not isinstance(selector, MCPTools)
                or selector.source_id not in self.sources
                for selector in selectors
            ):
                raise ConfigurationError("invalid_integration_configuration")
            if len({s.source_id for s in selectors}) != len(selectors):
                raise ConfigurationError("invalid_integration_configuration")
        self.paths, self.build = tuple(paths), build
        self.bindings = {}
        self.generations = []
        self.current = None
        self.owner = None
        self.task = None
        self.refresh_task = None
        self.stopped = False
        self.error = None
        self.lock = threading.RLock()

    async def refresh(self, *, initial=False):
        if self.stopped:
            raise ConfigurationError("integration_closed")
        if self.refresh_task is None or self.refresh_task.done():
            self.refresh_task = asyncio.create_task(self._refresh(initial))
            self.refresh_task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        return await asyncio.shield(self.refresh_task)

    async def _refresh(self, initial):
        self.changed.clear()
        snapshots = {}
        try:
            for name, source in self.sources.items():
                for retry in range(2):
                    try:
                        snapshots[name] = await source.listing()
                        break
                    except ConfigurationError as error:
                        if retry or error.reason != "tool_catalog_stale":
                            raise
                count = sum(len(data[2]) for data in snapshots.values())
                size = sum(
                    len(item[1])
                    for data in snapshots.values()
                    for item in data[2].values()
                )
                if (
                    count > self.config.max_tools
                    or size > self.config.max_catalog_bytes
                ):
                    raise ConfigurationError("tool_discovery_limit_exceeded")
            with self.lock:
                for key, binding in tuple(self.bindings.items()):
                    source_name, remote = key
                    snapshot = snapshots[source_name]
                    item = snapshot[2].get(remote)
                    if (
                        snapshot[1] != binding.epoch
                        or item is None
                        or item[1] != binding.raw
                    ):
                        binding.revoked = True
                        del self.bindings[key]
                selected = {path: [] for path in self.paths}
                for path, selectors in self.selections.items():
                    for selector in selectors:
                        available = snapshots[selector.source_id][2]
                        names = available if selector.names is None else selector.names
                        for name in names:
                            if name not in available:
                                if initial:
                                    raise ConfigurationError(
                                        "mcp_tool_selection_missing"
                                    )
                                continue
                            key = selector.source_id, name
                            binding = self.bindings.get(key)
                            if binding is None:
                                fact, raw = available[name]
                                binding = RemoteBinding(
                                    self.sources[selector.source_id], name, fact, raw
                                )
                                self.bindings[key] = binding
                            selected[path].append(binding)
                signature = tuple(
                    (path, tuple(id(b) for b in selected[path])) for path in self.paths
                )
                if self.current is not None and signature == self.signature:
                    self._check_combined_budget(self.current.bindings, count, size)
                    self._publish_sources(snapshots)
                    self.error = None
                    return self.current
            if self.owner is not None:
                with self.owner.lock:
                    active = {
                        id(getattr(root, "generation_catalog", root.catalog))
                        for root in self.owner.roots
                    }
                self.generations = [
                    g for g in self.generations if id(g.bindings) in active
                ]
                if len(self.generations) >= self.config.max_generations:
                    raise ConfigurationError("execution_busy")
            graph, catalog, controls = self.build(selected)
            self._check_combined_budget(catalog, count, size)
            if self.owner is not None:
                self.owner.catalog_delivery.publisher.select(catalog.facts)
            if self.owner is not None and self.owner.control is not None:
                from openeight.langchain._catalog import report_catalog

                try:
                    await self.owner._run_client_call(
                        lambda: report_catalog(
                            self.owner.catalog_delivery.publisher, catalog.facts
                        )
                    )
                except (APIError, ProtocolError, TransportError):
                    raise ConfigurationError("tool_publication_unavailable") from None
            with self.lock:
                self._publish_sources(snapshots)
                generation = Generation(
                    (self.current.number + 1) if self.current else 1,
                    graph,
                    catalog,
                    tuple(controls),
                )
                if self.owner is not None:
                    from openeight.langchain._catalog import _result_size

                    owner = self.owner
                    with owner.lock:
                        owner._ensure_open()
                        for control in controls:
                            control.owner = owner
                        owner.catalog, owner._graph = catalog, graph
                        delivery = owner.catalog_delivery
                        delivery.states = []
                        delivery.facts_bytes = sum(
                            _result_size(fact) for fact in catalog.facts
                        )
                        delivery.request_refresh()
                self.current = generation
                self.generations.append(generation)
                self.signature = signature
                self.error = None
                return generation
        except ConfigurationError as error:
            self.error = error.reason
            # A successful listing alone does not admit a failed candidate graph.
            for source in self.sources.values():
                source.invalidate()
            raise

    def _check_combined_budget(self, catalog, count, size):
        static = [
            binding for binding in catalog.entries.values() if binding.remote is None
        ]
        static_size = sum(
            len(binding.schema_bytes)
            + len(binding.facts.name.encode())
            + len(binding.facts.description.encode())
            for binding in static
        )
        if (
            len(static) + count > self.config.max_tools
            or static_size + size > self.config.max_catalog_bytes
        ):
            raise ConfigurationError("tool_discovery_limit_exceeded")

    def _publish_sources(self, snapshots):
        for name, (revision, epoch, _) in snapshots.items():
            source = self.sources[name]
            if source.failed or source.epoch != epoch or revision != source.revision:
                raise ConfigurationError("tool_catalog_stale")
        for source in self.sources.values():
            source.available = True

    def attach(self, owner):
        self.owner = owner
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.create_task(self._poll())

    def release(self):
        def prune():
            with self.owner.lock:
                active = {
                    id(getattr(root, "generation_catalog", root.catalog))
                    for root in self.owner.roots
                }
            self.generations = [
                g
                for g in self.generations
                if g is self.current or id(g.bindings) in active
            ]

        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(prune)

    async def _poll(self):
        while not self.stopped:
            next_refresh = min(
                (
                    s.fresh_until - self.config.request_timeout
                    for s in self.sources.values()
                ),
                default=time.monotonic() + self.config.refresh_interval,
            )
            try:
                await asyncio.wait_for(
                    self.changed.wait(), max(0.01, next_refresh - time.monotonic())
                )
            except TimeoutError:
                pass
            if self.stopped:
                return
            try:
                await self.refresh()
            except ConfigurationError:
                self.changed.clear()
                await asyncio.sleep(
                    min(self.config.request_timeout, self.config.refresh_interval)
                )

    async def admission(self):
        if self.error == "execution_busy" or any(
            not s.available or s.failed or time.monotonic() >= s.fresh_until
            for s in self.sources.values()
        ):
            try:
                await self.refresh()
            except ConfigurationError as error:
                if self.current is None or error.reason == "execution_busy":
                    raise
        return self.current

    def permit(self, binding, invocation, path, observed, call):
        with self.lock, binding.source.lock:
            binding.validate()
            entry = invocation.catalog.resolve(path, binding.fact.name)
            if entry is None or entry.remote is not binding:
                raise ConfigurationError("tool_binding_changed")
            if (
                observed != (invocation.owner.control is None)
                or invocation.owner is not self.owner
            ):
                raise ConfigurationError("integration_not_entered")
            binding.source.inflight += 1
            return Permit(
                binding, invocation, path, observed, call, binding.source.session
            )

    async def stop_refresh(self):
        self.stopped = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.refresh_task and not self.refresh_task.done():
            self.refresh_task.cancel()
            await asyncio.gather(self.refresh_task, return_exceptions=True)

    async def close(self):
        await self.stop_refresh()
        for source in self.sources.values():
            await source.close()
