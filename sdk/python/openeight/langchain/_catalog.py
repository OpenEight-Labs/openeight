"""Frozen local tool facts, independent of human policy configuration."""

import hashlib
import json
import threading
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, fields, is_dataclass, replace
from types import MappingProxyType

from openeight import ToolRegistration
from openeight._json import snapshot_json, thaw_json
from openeight.errors import ConfigurationError, ProtocolError

from ._compat import inspect_agent

CURRENT_INITIAL_BIND = ContextVar("openeight_initial_catalog_bind", default=None)


def tool_schema(tool):
    from ._frameworks._schemas import tool_schema as schema

    return schema(tool)


@dataclass(frozen=True)
class BoundTool:
    """Captured native tool identity and schema used to validate an actual attempt."""

    tool: object
    facts: ToolRegistration
    schema_bytes: bytes

    def matches(self, candidate, name):
        try:
            return (
                candidate is self.tool
                and name == self.facts.name
                and candidate.name == name
                and snapshot_json(tool_schema(candidate), "invalid_event")
                == self.schema_bytes
            )
        except Exception:
            return False


class BoundToolCatalog:
    """Capture immutable bound tool facts without executing tools."""

    def __init__(self, graph, middleware=None):
        capabilities = inspect_agent(graph, middleware)
        tools, completeness, diagnostic = (
            capabilities.tools,
            capabilities.discovery_completeness,
            capabilities.discovery_diagnostic,
        )
        entries, size = {}, 0
        conflicting = set()
        for tool in tools:
            try:
                facts = ToolRegistration(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=tool_schema(tool),
                )
                raw = snapshot_json(thaw_json(facts.input_schema), "invalid_event")
                size += (
                    len(raw)
                    + len(facts.name.encode())
                    + len(facts.description.encode())
                )
                if size > 1024 * 1024:
                    completeness, diagnostic = (
                        "partial",
                        "tool_discovery_limit_exceeded",
                    )
                    break
                if facts.name in conflicting or (
                    facts.name in entries and entries[facts.name].schema_bytes != raw
                ):
                    conflicting.add(facts.name)
                    entries.pop(facts.name, None)
                    completeness, diagnostic = "partial", "conflicting_tool_definitions"
                    continue
                entries[facts.name] = BoundTool(tool, facts, raw)
            except Exception:
                completeness, diagnostic = "partial", "unsupported_tool_definition"
        self.entries = MappingProxyType(entries)
        self.facts = tuple(entry.facts for entry in entries.values())
        self.completeness, self.diagnostic = completeness, diagnostic
        if middleware is not None and completeness != "complete":
            raise ConfigurationError(diagnostic) from None


def catalog_batches(facts):
    """Split frozen facts using the exact RegisterTools wire limits."""
    from openeight.client import _serialize_tool_facts

    def conditional_wire(item):
        wire = _serialize_tool_facts(item)
        # Reserve the UUID pair added after the initial catalog read.
        placeholder = "00000000-0000-4000-8000-000000000001"
        return dict(
            wire,
            expected_tool_id=item.expected_tool_id or placeholder,
            expected_current_version_id=item.expected_current_version_id or placeholder,
        )

    batch = []
    for fact in facts:
        candidate = [*batch, fact]
        size = len(
            json.dumps(
                {"tools": [conditional_wire(item) for item in candidate]},
                ensure_ascii=False,
            ).encode("utf-8")
        )
        if batch and (len(candidate) > 200 or size > 1048576):
            yield tuple(batch)
            batch = []
        batch.append(fact)
    if batch:
        yield tuple(batch)


class CatalogPublisher:
    """Freeze each publication precondition; conflicts need a new operator session."""

    def __init__(self, client, *, can_publish=None):
        self._can_publish = can_publish
        self.client = client
        self.lock = threading.RLock()
        self.entries = {}
        self.current = None
        self.selection_lock = threading.RLock()
        self.selected = None

    @staticmethod
    def fingerprint(fact):
        return hashlib.sha256(
            snapshot_json(
                {
                    "schema": thaw_json(fact.input_schema),
                    "description": fact.description,
                },
                "invalid_event",
            )
        ).digest()

    def select(self, facts):
        selected = {fact.name: self.fingerprint(fact) for fact in facts}
        if len(selected) != len(facts):
            raise ConfigurationError("conflicting_tool_definitions")
        with self.selection_lock:
            self.selected = selected

    def register_tools(self, facts, *, timeout=None):
        if self._can_publish is not None and not self._can_publish():
            raise ConfigurationError("durable_deployment_mismatch")
        with self.lock:
            with self.selection_lock:
                if self.selected is not None and any(
                    self.selected.get(fact.name) != self.fingerprint(fact)
                    and not (
                        (prior := self.entries.get(fact.name)) is not None
                        and prior[3] == "pending"
                        and prior[0] == self.fingerprint(fact)
                    )
                    for fact in facts
                ):
                    raise ConfigurationError("tool_binding_changed")
            if self.current is None:
                readiness = self.client.get_tool_readiness([], timeout=timeout)
                current = {}
                for item in readiness.tools:
                    if (
                        item.name in current
                        or not item.tool_id
                        or not item.tool_version_id
                    ):
                        raise ProtocolError(200)
                    current[item.name] = (item.tool_id, item.tool_version_id)
                self.current = current
            prepared = []
            staged = {}
            entry_count = len(self.entries)
            for fact in facts:
                fingerprint = self.fingerprint(fact)
                prior = self.entries.get(fact.name)
                if prior is not None and prior[3] == "conflict":
                    raise ConfigurationError("tool_registration_conflict")
                if prior is not None and prior[0] == fingerprint:
                    pair = prior[1:3]
                else:
                    if prior is not None and prior[3] == "pending":
                        raise ConfigurationError("tool_publication_unsettled")
                    if prior is None:
                        if entry_count >= 800:
                            raise ConfigurationError("tool_discovery_limit_exceeded")
                        entry_count += 1
                    pair = self.current.get(fact.name, (None, None))
                    staged[fact.name] = (fingerprint, pair[0], pair[1], "pending")
                prepared.append(
                    ToolRegistration(
                        name=fact.name,
                        description=fact.description,
                        input_schema=fact.input_schema,
                        expected_tool_id=pair[0],
                        expected_current_version_id=pair[1],
                    )
                )
            # Only sent facts can become unresolved; preparation failures leave no state.
            self.entries.update(staged)
            results = self.client.register_tools(tuple(prepared), timeout=timeout)
            if len(results) != len(prepared):
                raise ProtocolError(200)
            for index, result in enumerate(results):
                fact = prepared[index]
                fingerprint, expected_tool, expected_version, _ = self.entries[
                    fact.name
                ]
                if result.request_index != index:
                    raise ProtocolError(200)
                if result.status in {"registered", "replayed"}:
                    if (
                        result.tool is None
                        or result.tool_version is None
                        or result.tool.name != fact.name
                        or result.tool_version.tool_id != result.tool.id
                    ):
                        raise ProtocolError(200)
                    self.current[fact.name] = (result.tool.id, result.tool_version.id)
                    self.entries[fact.name] = (
                        fingerprint,
                        expected_tool,
                        expected_version,
                        "acknowledged",
                    )
                elif result.status in {"invalid", "conflict"}:
                    self.entries[fact.name] = (
                        fingerprint,
                        expected_tool,
                        expected_version,
                        "conflict",
                    )
                else:
                    raise ProtocolError(200)
            return results


def send_catalog_batch(client, facts, *, timeout=None):
    """Only explicit per-item acknowledgments establish schema publication."""
    results = client.register_tools(facts, timeout=timeout)
    if len(results) != len(facts) or any(
        result.request_index != index
        or result.status not in {"registered", "replayed", "invalid", "conflict"}
        or (
            result.status in {"registered", "replayed"}
            and (
                result.tool is None
                or result.tool_version is None
                or result.tool.name != facts[index].name
                or result.tool_version.tool_id != result.tool.id
            )
        )
        for index, result in enumerate(results)
    ):
        raise ProtocolError(200)
    return results


def report_catalog(client, facts, *, timeout=None):
    """Refresh a complete invocation contract set; rejection fails closed."""
    results = []
    for batch in catalog_batches(facts):
        returned = send_catalog_batch(client, batch, timeout=timeout)
        if any(result.status not in {"registered", "replayed"} for result in returned):
            raise ConfigurationError("invalid_tool_registration")
        offset = len(results)
        results.extend(
            replace(result, request_index=offset + result.request_index)
            for result in returned
        )
    return tuple(results)


def _result_size(value):
    def projection(item):
        if is_dataclass(item):
            return {
                field.name: projection(getattr(item, field.name))
                for field in fields(item)
            }
        if isinstance(item, Mapping):
            return {key: projection(child) for key, child in item.items()}
        if isinstance(item, (tuple, list)):
            return [projection(child) for child in item]
        return item

    return len(json.dumps(projection(value), ensure_ascii=False).encode()) + 256


class CatalogDelivery:
    """Owner-local publication generations; no Run or execution authority."""

    def __init__(self, owner):
        self.owner = owner
        self.publisher = CatalogPublisher(
            owner.client,
            can_publish=lambda: not getattr(owner, "_defer_catalog", False)
            or CURRENT_INITIAL_BIND.get() is owner,
        )
        self.publisher.select(owner.catalog.facts)
        owner.worker.catalog_publisher = self.publisher
        self.states = []
        self.results = {}
        self.result_bytes = 0
        self.facts_bytes = sum(_result_size(fact) for fact in owner.catalog.facts)
        self.generation = 0

    def request_refresh(self):
        if getattr(self.owner, "_defer_catalog", False):
            raise ConfigurationError("unsupported_execution_method")
        self.owner._ensure_open()
        with self.owner.lock:
            self.owner._ensure_open()
            if "pending" in self.states:
                return
            self.generation += 1
            self.states = ["pending"] * len(self.owner.catalog.facts)
            self.results = {}
            self.result_bytes = 0
            if not self.owner.worker.set_catalog_cache(self.facts_bytes):
                self.states = ["dropped"] * len(self.states)
                with self.owner.worker.condition:
                    self.owner.worker.dropped += 1
                    self.owner.worker.last_error = "queue_full"
                return
            offset = 0
            for facts in catalog_batches(self.owner.catalog.facts):
                indexes = tuple(range(offset, offset + len(facts)))
                generation = self.generation
                payload = (
                    facts,
                    lambda results,
                    indexes=indexes,
                    generation=generation: self.publish(generation, indexes, results),
                    lambda failed,
                    dropped,
                    indexes=indexes,
                    generation=generation: self.settle(
                        generation, indexes, failed, dropped
                    ),
                )
                if self.owner.worker.enqueue("catalog", "", payload) is None:
                    self.settle(generation, indexes, False, True)
                offset += len(facts)

    def publish(self, generation, indexes, results):
        self.owner._ensure_process()
        with self.owner.lock:
            if generation != self.generation:
                return
            for result in results:
                index = indexes[result.request_index]
                if self.states[index] != "pending":
                    continue
                if result.status in {"registered", "replayed"}:
                    self.states[index] = "acknowledged"
                    size = _result_size(result)
                    if not self.owner.worker.set_catalog_cache(
                        self.facts_bytes + self.result_bytes + size
                    ):
                        continue
                    self.result_bytes += size
                    self.results[index] = replace(result, request_index=index)
                else:
                    self.states[index] = "failed"
            snapshot = tuple(self.results.values())
            roots = tuple(self.owner.roots) if self.owner.control is None else ()
        # Invocation settlement takes its lock before owner.release().
        for invocation in roots:
            invocation.update_tool_contracts(snapshot, generation=generation)

    def settle(self, generation, indexes, failed, dropped):
        self.owner._ensure_process()
        with self.owner.lock:
            if generation != self.generation:
                return
            for index in indexes:
                if self.states[index] == "pending" and (failed or dropped):
                    self.states[index] = "dropped" if dropped else "failed"

    def snapshot(self):
        self.owner._ensure_process()
        with self.owner.lock:
            return tuple(self.results.values())

    def versioned_snapshot(self):
        self.owner._ensure_process()
        with self.owner.lock:
            return self.generation, tuple(self.results.values())

    def diagnostics(self):
        self.owner._ensure_process()
        with self.owner.lock:
            counts = {
                state: self.states.count(state)
                for state in ("pending", "acknowledged", "failed", "dropped")
            }
            state = "not_started"
            if (
                not self.owner.catalog.facts
                and self.owner.catalog.completeness == "complete"
            ):
                state = "empty"
            elif counts["pending"]:
                state = "pending"
            elif self.states:
                state = self.states[0] if len(set(self.states)) == 1 else "partial"
            return {
                "catalog_delivery_state": state,
                "catalog_total_tools": len(self.owner.catalog.facts),
                **{f"catalog_{key}_tools": value for key, value in counts.items()},
            }

    def release(self):
        self.owner._ensure_process()
        with self.owner.lock:
            self.results = {}
            self.result_bytes = 0
            self.owner.worker.set_catalog_cache(0)
