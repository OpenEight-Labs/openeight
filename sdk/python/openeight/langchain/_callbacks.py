"""Callback correlation with bounded public model and tool content."""

import json
import time

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphInterrupt

from openeight import Event
from openeight.telemetry import uuid7

from ._content import model_input, model_output, tool_output
from ._runtime import CURRENT_READ_CONTRACT, CURRENT_TOOL_ATTEMPT, utc_timestamp
from ._usage import normalize_result


def bounded_name(name, fallback="unknown", suffix=""):
    value = name if type(name) is str and name.strip() else fallback
    return (
        value.encode("utf-8")[: 128 - len(suffix.encode())].decode(
            "utf-8", errors="ignore"
        )
        + suffix
    )


def callback_name(serialized, explicit=None):
    if type(explicit) is str and explicit:
        return explicit
    if type(serialized) is dict:
        if type(serialized.get("name")) is str:
            return serialized["name"]
        identity = serialized.get("id")
        if type(identity) is list and identity and type(identity[-1]) is str:
            return identity[-1]
    return None


class ObservationCallbacks(BaseCallbackHandler):
    """Translate native callbacks into privacy-preserving Events for one Run."""

    run_inline = True

    def __init__(self, invocation, *, external_parent_run_id=None):
        self.invocation = invocation
        self.external_parent_run_id = external_parent_run_id

    def _record_safely(self, fn, *args, **kwargs):
        """Keep observation failures from changing application execution."""
        try:
            fn(*args, **kwargs)
        except Exception:
            if getattr(self.invocation, "durable", False):
                self.invocation.persistence_failed = True
            with self.invocation.owner.worker.condition:
                self.invocation.owner.worker.last_error = "callback_failed"

    def _start_observation(
        self,
        kind,
        run_id,
        parent_run_id,
        metadata=None,
        name=None,
        *,
        span_role=None,
        project_input=None,
    ):
        """Reserve correlation state, including placeholders for late parent callbacks."""
        invocation = self.invocation
        # User callbacks retain their native ancestry; SDK Events stay in this Run.
        if parent_run_id == self.external_parent_run_id:
            parent_run_id = None
        with invocation.lock:
            existing = invocation.entries.get(run_id)
            if (
                invocation.completed
                or invocation.callbacks_expired
                or existing is not None
                and existing.get("expired")
                or invocation.root_ended
                and not invocation.handlers
                or existing is not None
                and not existing.get("placeholder")
            ):
                return
            if existing is None:
                if len(invocation.entries) >= 1000:
                    if getattr(invocation, "durable", False):
                        invocation.observation_dropped = True
                    with invocation.owner.worker.condition:
                        invocation.owner.worker.dropped += 1
                        invocation.owner.worker.last_error = "callback_limit"
                    return
                if not invocation.owner.worker.reserve(512):
                    if getattr(invocation, "durable", False):
                        invocation.observation_dropped = True
                    return
            parent = invocation.entries.get(parent_run_id)
            if parent_run_id is not None and parent is None:
                if len(invocation.entries) < 999 and invocation.owner.worker.reserve(
                    512
                ):
                    parent = dict(
                        event_id=uuid7(), terminal=True, placeholder=True, charge=512
                    )
                    invocation.entries[parent_run_id] = parent
                elif getattr(invocation, "durable", False):
                    invocation.observation_dropped = True
            attempt = CURRENT_TOOL_ATTEMPT.get() if kind == "tool_call" else None
            suppressed = (
                attempt is not None
                and attempt.invocation is invocation
                and not attempt.callback_claimed
            )
            if suppressed:
                attempt.callback_claimed = True
                attempt.parent = parent["event_id"] if parent else None
            framework = {}
            if type(metadata) is dict:
                for key in ("ls_provider", "ls_model_name"):
                    value = metadata.get(key)
                    if type(value) is str and len(value.encode()) <= 256:
                        framework[key] = value
            # Callback name is used only to resolve one captured catalog name.
            tool_name = (
                name
                if kind == "tool_call" and name in invocation.owner.catalog.entries
                else None
            )
            if kind == "tool_call":
                from ._controller import CURRENT_BINDING

                binding = CURRENT_BINDING.get()
                if binding is not None:
                    tool_name = binding.facts.name
            read_contract = CURRENT_READ_CONTRACT.get()
            if read_contract is not None:
                contract_name = (
                    read_contract.catalog_name
                    if getattr(invocation, "durable", False)
                    and invocation.owner._integration == "deepagents"
                    else read_contract.tool.name
                )
                if contract_name != tool_name:
                    read_contract = None
            invocation.entries[run_id] = dict(
                event_id=attempt.event_id
                if suppressed
                else existing["event_id"]
                if existing
                else uuid7(),
                parent=parent["event_id"] if parent else None,
                kind=kind,
                name=bounded_name(
                    name,
                    {
                        "span": "chain",
                        "retriever": "retriever",
                        "llm_call": "model",
                        "tool_call": "tool",
                    }[kind],
                ),
                span_role=span_role,
                started=utc_timestamp(),
                monotonic=time.monotonic(),
                first=None,
                terminal=False,
                charge=512,
                pending_captures={},
                metadata=framework,
                tool_name=tool_name,
                tool_contract=read_contract or invocation.contracts.get(tool_name),
                suppressed=suppressed,
            )

            entry = invocation.entries[run_id]
            token = self._claim(entry, "input", project_input)
        if token is not None:
            self._capture(run_id, entry, "input", token, project_input)

    def _claim(self, entry, direction, project):
        if (
            project is None
            or entry.get("suppressed")
            or entry.get("expired")
            or entry["kind"] not in {"llm_call", "tool_call"}
            or not self.invocation.content_policy.enabled(direction)
        ):
            return None
        token = object()
        entry["pending_captures"][direction] = token
        return token

    def _capture(self, run_id, entry, direction, token, project):
        invocation = self.invocation
        finished = False
        try:
            value = invocation.capture_content(entry["kind"], direction, project)
            finished = True
        finally:
            with invocation.lock:
                if (
                    invocation.entries.get(run_id) is entry
                    and entry.get("pending_captures", {}).get(direction) is token
                ):
                    del entry["pending_captures"][direction]
                    if not finished:
                        # Cancellation/termination invalidates competing claims too.
                        entry["expired"] = entry["terminal"] = True
                        entry["pending_captures"].clear()
                        entry.pop("finish_event", None)
                    elif value is not None:
                        charge = len(json.dumps(value, ensure_ascii=False).encode())
                        if invocation.owner.worker.reserve(entry["charge"] + charge):
                            invocation.owner.worker.release(entry["charge"])
                            entry["charge"] += charge
                            entry[direction + "_content"] = value
                    if finished:
                        self._publish(entry)
            invocation.complete_when_settled()

    def _publish(self, entry):
        # The caller holds invocation.lock. No application code executes here.
        if (
            entry.get("expired")
            or entry.get("pending_captures")
            or "finish_event" not in entry
        ):
            return
        args = entry.pop("finish_event")
        self.invocation.enqueue_event(
            entry["event_id"],
            Event(
                **args,
                input=entry.pop("input_content", None),
                output=entry.pop("output_content", None),
            ),
        )

    def _finish_observation(
        self, run_id, error=None, response=None, project_output=None
    ):
        invocation = self.invocation
        with invocation.lock:
            entry = invocation.entries.get(run_id)
            if invocation.completed or entry is None or entry["terminal"]:
                return
            if error == "interrupted":
                parent_id = entry.get("parent")
                seen = set()
                by_event = {
                    value["event_id"]: value for value in invocation.entries.values()
                }
                while parent_id and parent_id not in seen:
                    seen.add(parent_id)
                    ancestor = by_event.get(parent_id)
                    if ancestor is None:
                        break
                    if ancestor.get("kind") == "span":
                        ancestor["interrupted"] = True
                    parent_id = ancestor.get("parent")
            elif error is None and entry.get("interrupted"):
                error = "interrupted"
            entry["terminal"] = True
            if entry["suppressed"]:
                invocation.complete_when_settled()
                return
            latency = max(0, int((time.monotonic() - entry["monotonic"]) * 1000))
            metrics = {}
            if entry["kind"] == "llm_call" and response is not None:
                metrics, diagnostics = normalize_result(response, entry["metadata"])
                if diagnostics:
                    with invocation.owner.worker.condition:
                        invocation.owner.worker.last_error = diagnostics[0]
                if entry["first"] is not None:
                    metrics["ttft_ms"] = min(
                        latency,
                        max(0, int((entry["first"] - entry["monotonic"]) * 1000)),
                    )
            links = {}
            tool_name = entry["tool_name"]
            if tool_name is not None:
                links["tool_name"] = tool_name
                contract = entry.get("tool_contract")
                if (
                    contract is not None
                    and getattr(invocation, "durable", False)
                    and invocation.owner._integration == "deepagents"
                ):
                    if contract.recovery_kind == "read":
                        links.update(
                            tool_id=contract.tool_id,
                            tool_version_id=contract.tool_version_id,
                            attributes={
                                "openeight.tool.governance_revision_id": contract.readiness.governance_revision_id
                            },
                        )
                elif contract is not None and (
                    not contract.tool.governed or contract.tool.capability == "read"
                ):
                    links.update(
                        tool_id=contract.tool.id,
                        tool_version_id=contract.tool_version.id,
                        attributes={
                            "openeight.tool.governance_revision_id": contract.tool_version.governance_revision_id
                        },
                    )
            name = entry["name"]
            if tool_name:
                name = bounded_name(tool_name)
            elif entry["kind"] == "llm_call" and metrics.get("model"):
                name = bounded_name(
                    "/".join(
                        str(metrics[key])
                        for key in ("provider", "model")
                        if metrics.get(key)
                    )
                )
            kind = "span" if entry["kind"] == "retriever" else entry["kind"]
            entry["finish_event"] = dict(
                name=name,
                observation_kind=kind,
                operation_kind=invocation.operation,
                occurred_at=entry["started"],
                outcome="interrupted"
                if error == "interrupted"
                else "error"
                if error
                else "success",
                parent_event_id=entry["parent"],
                error={"kind": error} if error and error != "interrupted" else None,
                span_role=entry["span_role"],
                latency_ms=latency,
                **metrics,
                **links,
            )
            token = self._claim(entry, "output", None if error else project_output)
            if token is None:
                self._publish(entry)
        if token is not None:
            self._capture(run_id, entry, "output", token, project_output)
        invocation.complete_when_settled()

    def on_chain_start(
        self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs
    ):
        metadata = kwargs.get("metadata") or {}
        name = callback_name(serialized, kwargs.get("name"))
        role = (
            "node"
            if type(metadata) is dict and metadata.get("langgraph_node") == name
            else "graph"
            if name == "LangGraph"
            else None
        )
        self._record_safely(
            self._start_observation,
            "span",
            run_id,
            parent_run_id,
            name=name,
            span_role=role,
        )

    def on_chain_end(self, outputs, *, run_id, **kwargs):
        self._record_safely(self._finish_observation, run_id)

    def on_chain_error(self, error, *, run_id, **kwargs):
        self._record_safely(
            self._finish_observation,
            run_id,
            "interrupted" if isinstance(error, GraphInterrupt) else "chain_error",
        )

    def on_retriever_start(
        self, serialized, query, *, run_id, parent_run_id=None, **kwargs
    ):
        self._record_safely(
            self._start_observation,
            "retriever",
            run_id,
            parent_run_id,
            name=callback_name(serialized, kwargs.get("name")),
            span_role="retriever",
        )

    def on_retriever_end(self, documents, *, run_id, **kwargs):
        self._record_safely(self._finish_observation, run_id)

    def on_retriever_error(self, error, *, run_id, **kwargs):
        self._record_safely(self._finish_observation, run_id, "retriever_error")

    def on_chat_model_start(
        self,
        serialized,
        messages,
        *,
        run_id,
        parent_run_id=None,
        metadata=None,
        **kwargs,
    ):
        self._record_safely(
            self._start_observation,
            "llm_call",
            run_id,
            parent_run_id,
            metadata,
            name=callback_name(serialized, kwargs.get("name")),
            project_input=lambda: model_input(messages),
        )

    def on_llm_start(
        self,
        serialized,
        prompts,
        *,
        run_id,
        parent_run_id=None,
        metadata=None,
        **kwargs,
    ):
        self._record_safely(
            self._start_observation,
            "llm_call",
            run_id,
            parent_run_id,
            metadata,
            name=callback_name(serialized, kwargs.get("name")),
            project_input=lambda: {"prompts": prompts},
        )

    def on_llm_new_token(self, token, *, run_id, **kwargs):
        with self.invocation.lock:
            entry = self.invocation.entries.get(run_id)
            if entry is not None and not entry["terminal"] and entry["first"] is None:
                entry["first"] = time.monotonic()

    def on_llm_end(self, response, *, run_id, **kwargs):
        self._record_safely(
            self._finish_observation,
            run_id,
            response=response,
            project_output=lambda: model_output(response),
        )

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._record_safely(self._finish_observation, run_id, "model_error")

    def on_tool_start(
        self, serialized, input_str, *, run_id, parent_run_id=None, **kwargs
    ):
        name = serialized.get("name") if type(serialized) is dict else None
        self._record_safely(
            self._start_observation,
            "tool_call",
            run_id,
            parent_run_id,
            name=name,
            project_input=lambda: kwargs.get("inputs")
            if kwargs.get("inputs") is not None
            else input_str,
        )

    def on_tool_end(self, output, *, run_id, **kwargs):
        self._record_safely(
            self._finish_observation,
            run_id,
            error="tool_error"
            if isinstance(output, ToolMessage) and output.status == "error"
            else None,
            project_output=lambda: tool_output(output),
        )

    def on_tool_error(self, error, *, run_id, **kwargs):
        self._record_safely(self._finish_observation, run_id, "tool_error")
