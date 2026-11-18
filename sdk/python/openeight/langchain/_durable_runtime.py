"""Durable invocation ownership and atomic governed-attempt settlement."""

import asyncio
import json
import time
from concurrent.futures import Future
from contextvars import copy_context
from copy import deepcopy
from dataclasses import dataclass
from uuid import uuid4

from openeight import ConfigurationError, Event, ProtocolError
from openeight._framework._types import CapturedGeneratedResult, NativeToolResult
from openeight.telemetry import uuid7

from ._callbacks import bounded_name
from ._catalog import report_catalog
from ._compat import copy_tool_call_request
from ._content import tool_output
from ._deployment import checked_record
from ._injected import FrozenInjectedTool, hidden_action_key
from ._runtime import (
    CURRENT_READ_CONTRACT,
    CURRENT_TOOL_ATTEMPT,
    AgentInvocation,
    tool_parent_event,
    utc_timestamp,
)
from .durable import (
    AttemptSettlement,
    AttemptTransition,
    DurableAttempt,
    DurableObserveRecord,
    DurableTransition,
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(eq=False, repr=False)
class _DurableToolCorrelation:
    invocation: "DurableInvocation"
    event_id: str
    callback_claimed: bool = False
    parent: str | None = None


@dataclass(frozen=True)
class NativeToolIdentity:
    tool_call_id: str
    tool_name: str


def native_tool_identity(attempt, *, invocation, continuation=None):
    try:
        native = json.loads(attempt.native_call_json)
        thread = checked_record(
            invocation.store.load(invocation.thread_id),
            kind="thread",
            thread_id=invocation.thread_id,
            allow_none=True,
        )
        if getattr(invocation.owner, "_integration", "langchain") == "deepagents":
            from openeight.deepagents._continuation import frozen_native_identity

            if thread is None or thread.deployment_id != attempt.deployment_id:
                raise ValueError
            return NativeToolIdentity(
                *frozen_native_identity(
                    attempt, invocation=invocation, continuation=continuation
                )
            )
        if (
            continuation is not None
            or attempt.thread_id != invocation.thread_id
            or thread is None
            or attempt.deployment_id != thread.deployment_id
            or type(native.get("id")) is not str
            or not native["id"]
            or type(native.get("name")) is not str
            or not native["name"]
            or native["id"] != attempt.tool_call_id
            or native["name"] != attempt.tool_name
        ):
            raise ValueError
        return NativeToolIdentity(native["id"], native["name"])
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ConfigurationError("durable_attempt_conflict") from None


def result_message(attempt, *, invocation, content, status, continuation=None):
    identity = native_tool_identity(
        attempt, invocation=invocation, continuation=continuation
    )
    return invocation.owner._compatibility.tool_message(
        NativeToolResult(identity.tool_call_id, identity.tool_name, content, status)
    )


def tool_message(attempt, *, invocation, continuation=None):
    value = json.loads(attempt.result_json)
    return result_message(
        attempt,
        invocation=invocation,
        content=value["content"],
        status=value["status"],
        continuation=continuation,
    )


def freeze_message(value, *, invocation, attempt, continuation=None):
    result = invocation.owner._compatibility.tool_result(value)
    identity = native_tool_identity(
        attempt, invocation=invocation, continuation=continuation
    )
    if (
        result.tool_call_id != identity.tool_call_id
        or result.tool_name != identity.tool_name
        or len(result.content.encode()) > 65536
    ):
        raise ConfigurationError("invalid_tool_result")
    return canonical({"content": result.content, "status": result.status})


class DurableInvocation(AgentInvocation):
    durable = True

    def __init__(self, owner, thread):
        super().__init__(owner, "invoke")
        self.thread_id = thread.id
        self.deployment_id = thread.deployment_id
        self.run_id, self.started = thread.run_id, thread.run_started_at
        self.store = owner.control.durable.store
        self.execution_lock = asyncio.Lock()
        self.execution_tail = None
        self.persistence_failed = False
        self.native_cancelled = False
        self.observation_dropped = False
        self.recovering = False
        self.callback_tasks = []
        self.callback_loop = asyncio.get_running_loop()
        self.callback_context = copy_context()
        self.recovery_namespaces = frozenset()
        self.resume_decision_ids = None
        self.generated_replays = {}
        self.generated_recovery_outcomes = {}
        self.checkpoint_snapshot = None
        if owner._integration == "deepagents":
            self.native_observations = owner._compatibility.execution_observations(
                thread_id=thread.id
            )

    def adopt_checkpoint(self, snapshot):
        if self.owner._integration != "deepagents":
            return
        self.checkpoint_snapshot = snapshot
        self.native_observations = self.owner._compatibility.execution_observations(
            thread_id=self.thread_id, snapshot=snapshot
        )
        self.recovery_namespaces = (
            self.owner._compatibility.recovery_namespaces(snapshot)
            if self.recovering
            else frozenset()
        )

    async def prepare_generated_recovery(self, snapshot):
        if self.owner._integration != "deepagents":
            return
        for attempt in checked_record(
            self.store.attempts(self.thread_id),
            kind="attempt",
            thread_id=self.thread_id,
        ):
            if attempt.status in {"succeeded", "failed", "blocked"}:
                continue
            continuation = self.continuation(attempt)
            path = tuple(json.loads(continuation.child_path_json))
            contracts = [
                c
                for c in self.owner._bound_catalog.contracts
                if c.child_path == path and c.catalog_name == attempt.tool_name
            ]
            if (
                len(contracts) != 1
                or contracts[0].tool_id != attempt.tool_id
                or contracts[0].tool_version_id != attempt.tool_version_id
                or contracts[0].readiness.policy_contract_set_digest
                != attempt.policy_contract_set_digest
            ):
                raise ConfigurationError("governance_not_ready")
        for attempt in checked_record(
            self.store.attempts(self.thread_id),
            kind="attempt",
            thread_id=self.thread_id,
        ):
            continuation = self.continuation(attempt)
            if continuation.kind not in {"state_write", "delegation"}:
                continue
            receipt = self.store.generated_state_receipt(
                self.thread_id, attempt.tool_call_id
            )
            if receipt is None and attempt.status not in {
                "executing",
                "reconciliation_required",
                "succeeded",
                "failed",
            }:
                continue
            outcome = await self.owner._compatibility.recover_generated(
                self, attempt, continuation, snapshot
            )
            if outcome not in {"restored", "resume_required"}:
                raise ConfigurationError("durable_reconciliation_required")
            self.generated_recovery_outcomes[attempt.tool_call_id] = outcome

    def continuation(self, attempt):
        if self.owner._integration != "deepagents":
            return None
        value = self.store.continuation(self.thread_id, attempt.tool_call_id)
        if value is None:
            raise ConfigurationError("durable_attempt_conflict")
        # Validate the record before any reconstruction or atomic settlement.
        native_tool_identity(attempt, invocation=self, continuation=value)
        return value

    async def storage_call(self, function):
        async with self.owner._graph.checkpointer.lock:
            return function()

    def start(self):
        if self.owner._integration == "deepagents":
            catalog = self.owner._bound_catalog
            if catalog is None:
                raise ConfigurationError("durable_deployment_unbound")
            self.contracts = {
                contract.catalog_name: contract for contract in catalog.contracts
            }
            return
        self.update_tool_contracts(
            report_catalog(
                self.owner.catalog_delivery.publisher, self.owner.catalog.facts
            )
        )

    def mark_observation_incomplete(self):
        thread = checked_record(
            self.store.load(self.thread_id),
            kind="thread",
            thread_id=self.thread_id,
            allow_none=True,
        )
        if thread.observation_incomplete:
            return
        checked_record(
            self.store.transition(
                self.thread_id,
                thread.revision,
                DurableTransition(observation_incomplete=True),
            ),
            kind="thread",
            thread_id=self.thread_id,
        )

    def enqueue_event(self, event_id, event):
        payload = event._as_write()
        for field in ("input_json", "output_json"):
            if payload.get(field) == "null":
                payload.pop(field, None)
        payload.update(id=event_id, run_id=self.run_id)
        record = DurableObserveRecord(
            event_id,
            self.thread_id,
            "event",
            canonical(payload),
            payload["occurred_at"],
        )

        async def persist():
            try:
                await self.storage_call(
                    lambda: self.store.enqueue_callback_event(self.thread_id, record)
                )
            except BaseException:
                self.persistence_failed = True
                raise

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is self.callback_loop:
            task = self.callback_loop.create_task(
                persist(), context=self.callback_context.copy()
            )
        else:
            task = self.callback_context.copy().run(
                asyncio.run_coroutine_threadsafe, persist(), self.callback_loop
            )
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        self.callback_tasks.append(task)

    def complete_when_settled(self):
        # The command driver owns Run completion after all persisted evidence.
        return

    async def settle_callbacks(self):
        if self.observation_dropped or self.persistence_failed:
            await self.storage_call(self.mark_observation_incomplete)
        deadline = (
            asyncio.get_running_loop().time() + self.owner.delivery.late_callback_grace
        )
        while True:
            with self.lock:
                pending = any(
                    not entry["terminal"]
                    or entry.get("placeholder")
                    or entry.get("pending_captures")
                    for entry in self.entries.values()
                )
                if not pending:
                    self.callbacks_expired = True
                    tasks = tuple(self.callback_tasks)
                    break
            if asyncio.get_running_loop().time() >= deadline:
                self.expire_callbacks()
                await self.storage_call(self.mark_observation_incomplete)
                with self.lock:
                    tasks = tuple(self.callback_tasks)
                break
            await asyncio.sleep(0.01)
        if tasks:
            await asyncio.gather(
                *(
                    asyncio.wrap_future(task) if isinstance(task, Future) else task
                    for task in tasks
                )
            )
        if self.persistence_failed:
            raise ConfigurationError("durable_persistence_failed")

    def release(self):
        with self.lock:
            self.root_ended = True
            self.completed = True
            for entry in self.entries.values():
                self.owner.worker.release(entry["charge"])
            self.entries.clear()
        self.owner.release(self)

    async def drain(self):
        for record in checked_record(
            self.store.pending(self.thread_id),
            kind="observation",
            thread_id=self.thread_id,
            run_id=self.run_id,
        ):
            payload = json.loads(record.payload_json)
            await self.owner._run_client_call(
                lambda payload=payload,
                kind=record.kind: self.owner.client.write_telemetry(
                    runs=[payload] if kind != "event" else [],
                    events=[payload] if kind == "event" else [],
                )
            )
            await self.storage_call(
                lambda: self.store.acknowledge(self.thread_id, record.id)
            )

    def _validate_call(self, request, *, call_context=None):
        if self.owner._integration == "deepagents":
            from openeight.deepagents._durable_calls import validate_call

            return validate_call(self, request, call_context=call_context)
        if call_context is not None:
            raise ConfigurationError("invalid_resume_correlation")
        call = deepcopy(request.tool_call)
        name = call.get("name")
        entry = self.owner.catalog.entries.get(name)
        contract = self.contracts.get(name)
        if entry is None or not entry.matches(request.tool, name):
            raise ConfigurationError("unsupported_tool_definition")
        if (
            type(call.get("id")) is not str
            or not call["id"]
            or len(call["id"].encode()) > 256
            or type(call.get("args")) is not dict
        ):
            raise ConfigurationError("invalid_input")
        if "idempotency_key" in call["args"]:
            raise ConfigurationError("invalid_input")
        # Pydantic validates types, while the public property set closes schemas
        # even for native tools that would otherwise ignore extra fields.
        public_schema = request.tool.tool_call_schema
        properties = public_schema.model_json_schema().get("properties", {})
        if set(call["args"]) != set(properties):
            raise ConfigurationError("invalid_input")
        try:
            validated = public_schema.model_validate(
                call["args"], strict=True
            ).model_dump()
            if canonical(validated) != canonical(call["args"]):
                raise ValueError
        except Exception:
            raise ConfigurationError("invalid_input") from None
        return call, name, contract

    def _prepare(self, request, execution_contract=None, *, call_context=None):
        if self.owner._integration == "deepagents":
            from openeight.deepagents._durable_calls import prepare

            return prepare(self, request, execution_contract, call_context=call_context)
        if call_context is not None:
            raise ConfigurationError("invalid_resume_correlation")
        call, name, contract = self._validate_call(request)
        previous = checked_record(
            self.store.get_attempt(self.thread_id, call["id"]),
            kind="attempt",
            thread_id=self.thread_id,
            tool_call_id=call["id"],
            allow_none=True,
        )
        write = hidden_action_key(request.tool)
        if previous:
            effective = deepcopy(call)
            if write:
                effective["args"]["idempotency_key"] = previous.idempotency_key
            if (
                previous.native_call_json != canonical(effective)
                or previous.tool_name != name
                or write != (previous.idempotency_key is not None)
            ):
                raise ConfigurationError("durable_attempt_conflict")
            return previous, effective, contract
        contract = execution_contract
        if (
            contract is None
            or not contract.tool.governed
            or contract.tool_version.tool_contract_digest is None
        ):
            raise ConfigurationError("governance_unconfigured")
        readiness = contract.execution_readiness
        if readiness is None or not readiness.eligible or not readiness.policy_ready:
            raise ConfigurationError("governance_not_ready")
        try:
            from openeight._json import decision_envelope
            from openeight.tools import collect_selected_values

            selected = decision_envelope(
                {},
                readiness.policy_contract_set_digest,
                collect_selected_values(call["args"], readiness),
            ).decode()
        except Exception:
            raise ConfigurationError("invalid_input") from None
        if self.recovering:
            raise ConfigurationError("durable_attempt_missing")
        if (
            len(
                checked_record(
                    self.store.attempts(self.thread_id),
                    kind="attempt",
                    thread_id=self.thread_id,
                )
            )
            + checked_record(
                self.store.load(self.thread_id),
                kind="thread",
                thread_id=self.thread_id,
                allow_none=True,
            ).read_calls
            >= self.owner.control.durable.max_tool_attempts
        ):
            raise ConfigurationError("durable_limit_exhausted")
        key = str(uuid4()) if write else None
        if key:
            call["args"]["idempotency_key"] = key
        decision_id, event_id, ts = uuid7(), uuid7(), utc_timestamp()
        event = Event(
            bounded_name(name),
            "tool_call",
            "invoke",
            ts,
            "success",
            parent_event_id=tool_parent_event(self, request),
            decision_id=decision_id,
            input=json.loads(selected),
            tool_id=contract.tool.id,
            tool_version_id=contract.tool_version.id,
            tool_name=name,
        )._as_write()
        event.pop("output_json", None)
        event.update(id=event_id, run_id=self.run_id)
        thread = checked_record(
            self.store.load(self.thread_id),
            kind="thread",
            thread_id=self.thread_id,
            allow_none=True,
        )
        attempt = DurableAttempt(
            thread.deployment_id,
            decision_id,
            self.thread_id,
            call["id"],
            name,
            contract.tool.id,
            contract.tool_version.id,
            readiness.policy_contract_set_digest,
            ts,
            event_id,
            canonical(call),
            selected,
            canonical(event),
            ts,
            ts,
            idempotency_key=key,
        )
        return (
            checked_record(
                self.store.prepare(attempt),
                kind="attempt",
                thread_id=attempt.thread_id,
                tool_call_id=attempt.tool_call_id,
                decision_id=attempt.decision_id,
            ),
            call,
            contract,
        )

    def lifecycle_record(self, attempt, transition):
        saved = (
            attempt.pause_event_json
            if transition == "paused"
            else attempt.resume_event_json
        )
        if saved is not None:
            return None
        event = Event(
            bounded_name(attempt.tool_name, suffix="." + transition),
            "lifecycle",
            "unspecified",
            utc_timestamp(),
            "interrupted" if transition == "paused" else "unspecified",
            decision_id=attempt.decision_id,
            tool_id=attempt.tool_id,
            tool_version_id=attempt.tool_version_id,
            tool_name=attempt.tool_name,
            parent_event_id=json.loads(attempt.event_template_json).get(
                "parent_event_id"
            ),
        )._as_write()
        event.update(id=uuid7(), run_id=self.run_id)
        return DurableObserveRecord(
            event["id"], self.thread_id, "event", canonical(event), event["occurred_at"]
        )

    async def authorize(self, attempt):
        request = dict(
            decision_id=attempt.decision_id,
            run_id=self.run_id,
            tool_id=attempt.tool_id,
            tool_version_id=attempt.tool_version_id,
            source_time=attempt.source_time,
            **json.loads(attempt.decision_input_json),
        )
        decision = await self.owner._run_client_call(
            lambda: self.owner.client.decide(**request)
        )
        if (
            decision.decision_id != attempt.decision_id
            or decision.run_id != self.run_id
        ):
            raise ProtocolError()
        if decision.needs_approval:
            state = await self.owner._run_client_call(
                lambda: self.owner.client.approval_state(attempt.decision_id)
            )
            if state == "pending":
                if attempt.status != "waiting":
                    attempt = await self.storage_call(
                        lambda: checked_record(
                            self.store.transition_attempt(
                                self.thread_id,
                                attempt.decision_id,
                                attempt.revision,
                                AttemptTransition(
                                    "waiting",
                                    event=self.lifecycle_record(attempt, "paused"),
                                ),
                            ),
                            kind="attempt",
                            thread_id=self.thread_id,
                            decision_id=attempt.decision_id,
                        )
                    )
                self.owner._compatibility.pause_for_approval(attempt.decision_id)
                # Resumption values are correlation only; refresh authorization.
                return await self.authorize(attempt)
            if state in {"rejected", "expired"}:
                return "approval_" + state
            if state != "approved":
                raise ProtocolError()
            decision = await self.owner._run_client_call(
                lambda: self.owner.client.decide(**request)
            )
            if (
                decision.decision_id != attempt.decision_id
                or decision.run_id != self.run_id
            ):
                raise ProtocolError()
            if decision.allowed or decision.needs_approval and decision.replayed:
                return None
            return decision.verdict
        return None if decision.allowed else decision.verdict

    async def settle(
        self,
        attempt,
        result,
        *,
        blocked=False,
        reason=None,
        execution_started_at=None,
        execution_latency_ms=None,
        recovered=False,
        generated=None,
    ):
        if generated is not None:
            if (
                self.owner._integration != "deepagents"
                or blocked
                or not isinstance(generated, CapturedGeneratedResult)
                or generated.result != self.owner._compatibility.tool_result(result)
                or self.continuation(attempt).kind not in {"state_write", "delegation"}
            ):
                raise ConfigurationError("invalid_tool_result")
        status = (
            "blocked"
            if blocked
            else "failed"
            if self.owner._compatibility.tool_result(result).status == "error"
            else "succeeded"
        )
        record = None
        if not blocked:
            event = json.loads(attempt.event_template_json)
            if recovered:
                event["occurred_at"] = utc_timestamp()
                event["attributes_json"] = canonical(
                    {
                        **json.loads(event.get("attributes_json", "{}")),
                        "timing_source": "receipt_recovery",
                    }
                )
                event.pop("latency_ms", None)
            else:
                if execution_started_at is None or execution_latency_ms is None:
                    raise ConfigurationError("execution_timing_missing")
                event["occurred_at"] = execution_started_at
                event["latency_ms"] = execution_latency_ms
            event["outcome"] = "error" if status == "failed" else "success"
            event.pop("output_json", None)
            if status == "succeeded":
                output = self.capture_content(
                    "tool_call", "output", lambda: tool_output(result)
                )
                if output is not None:
                    event["output_json"] = json.dumps(
                        output,
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
            if status == "failed":
                event["error_json"] = canonical({"kind": "tool_error"})
                event.pop("output_json", None)
            record = DurableObserveRecord(
                attempt.event_id,
                self.thread_id,
                "event",
                canonical(event),
                event["occurred_at"],
            )

        def persist():
            settlement = AttemptSettlement(
                status,
                freeze_message(
                    result,
                    invocation=self,
                    attempt=attempt,
                    continuation=self.continuation(attempt),
                ),
                reason,
                record,
            )
            if generated is not None:
                return self.store.settle_generated_attempt(
                    self.thread_id,
                    attempt.tool_call_id,
                    attempt.revision,
                    settlement,
                    generated.receipt,
                )
            return checked_record(
                self.store.settle_attempt(
                    self.thread_id,
                    attempt.decision_id,
                    attempt.revision,
                    settlement,
                ),
                kind="attempt",
                thread_id=self.thread_id,
                decision_id=attempt.decision_id,
            )

        return await self.storage_call(persist)

    async def _owned_tool_call(self, request, handler, *, call_context):
        task = asyncio.create_task(
            self._tool_call_serial(request, handler, call_context=call_context)
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with self.lock:
                self.native_cancelled = True
            # Native sibling cancellation cannot release an admitted executor handler.
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

    async def tool_call(self, request, handler, *, call_context=None):
        deep = self.owner._integration == "deepagents"
        if (deep and call_context is None) or (not deep and call_context is not None):
            raise ConfigurationError("invalid_resume_correlation")
        if deep and call_context.kind == "delegation":
            # The delegated child must never wait on its own parent's handler barrier.
            return await self._owned_tool_call(
                request, handler, call_context=call_context
            )
        # Reserve ordering briefly; user handlers and redactors run without locks.
        async with self.execution_lock:
            previous = self.execution_tail
            settled = asyncio.get_running_loop().create_future()
            self.execution_tail = settled
        try:
            if previous is not None:
                await asyncio.shield(previous)
            return await self._owned_tool_call(
                request, handler, call_context=call_context
            )
        finally:
            # A cancelled waiter must not let its successor overtake its predecessor.
            if previous is not None and not previous.done():
                previous.add_done_callback(lambda _: settled.set_result(None))
            else:
                settled.set_result(None)

    async def _tool_call_serial(self, request, handler, *, call_context=None):
        if self.native_cancelled:
            raise ConfigurationError("durable_reconciliation_required")
        deep = self.owner._integration == "deepagents"
        context_arguments = {"call_context": call_context} if deep else {}
        bound = None
        if deep:
            from openeight.deepagents._admission import _validate_durable_admission
            from openeight.deepagents._continuation import durable_attempt_key

            catalog = await _validate_durable_admission(self.owner)
            with self.lock:
                self.contracts = {c.catalog_name: c for c in catalog.contracts}
            _, _, bound = self._validate_call(request, **context_arguments)
            call_id = durable_attempt_key(call_context, request.tool_call.get("id"))
        else:
            call_id = request.tool_call.get("id")
        previous = (
            checked_record(
                self.store.get_attempt(self.thread_id, call_id),
                kind="attempt",
                thread_id=self.thread_id,
                tool_call_id=call_id,
                allow_none=True,
            )
            if isinstance(call_id, str)
            else None
        )
        if previous is None and not deep:
            self.update_tool_contracts(
                await self.owner._run_client_call(
                    lambda: report_catalog(
                        self.owner.catalog_delivery.publisher,
                        self.owner.catalog.facts,
                        timeout=1.0,
                    )
                )
            )
        if previous is None:
            # Reads use ordinary graph checkpoint/replay and callback telemetry;
            # they never create a governed durable attempt or receipt.
            contract = (
                bound if deep else self.contracts.get(request.tool_call.get("name"))
            )
            is_read = contract is not None and (
                contract.recovery_kind == "read"
                if deep
                else contract.tool.capability == "read"
            )
            if is_read:
                return await self._execute_read_call(
                    request,
                    handler,
                    contract=contract,
                    deep=deep,
                    call_context=call_context,
                    context_arguments=context_arguments,
                )
        try:
            execution_contract = bound
            if previous is None and not deep:
                execution_contract = await self.owner._run_client_call(
                    lambda: self.owner.control._fresh_contract(request)
                )
            attempt, call, contract = await self.storage_call(
                lambda: self._prepare(request, execution_contract, **context_arguments)
            )
        except ConfigurationError as error:
            if (
                error.reason
                not in {
                    "governance_unconfigured",
                    "governance_not_ready",
                }
                or deep
                and previous is not None
            ):
                raise
            return self.owner._compatibility.tool_message(
                NativeToolResult(
                    request.tool_call["id"],
                    request.tool_call["name"],
                    "Action blocked: this tool is awaiting governance configuration or policy readiness. The tool was not executed.",
                    "error",
                )
            )
        continuation = self.continuation(attempt)
        generated = continuation is not None and continuation.kind in {
            "state_write",
            "delegation",
        }
        if attempt.status in {"succeeded", "failed", "blocked"}:
            return self._replay_settled_attempt(
                attempt,
                continuation=continuation,
                generated=generated,
            )
        if generated and attempt.status in {"executing", "reconciliation_required"}:
            if (
                self.generated_recovery_outcomes.get(attempt.tool_call_id)
                != "resume_required"
            ):
                raise ConfigurationError("durable_reconciliation_required")
        elif attempt.status in {"executing", "reconciliation_required"}:
            if not attempt.idempotency_key:
                raise ConfigurationError("durable_reconciliation_required")
            reconciliation = await self.owner.control.durable.reconcile(attempt)
            if reconciliation.status == "confirmed":
                if attempt.status != "executing":
                    attempt = await self.storage_call(
                        lambda: checked_record(
                            self.store.transition_attempt(
                                self.thread_id,
                                attempt.decision_id,
                                attempt.revision,
                                AttemptTransition("executing"),
                            ),
                            kind="attempt",
                            thread_id=self.thread_id,
                            decision_id=attempt.decision_id,
                        )
                    )
                value = json.loads(reconciliation.result_json)
                result = result_message(
                    attempt,
                    invocation=self,
                    continuation=self.continuation(attempt),
                    content=value["content"],
                    status=value["status"],
                )
                return tool_message(
                    await self.settle(attempt, result, recovered=True),
                    invocation=self,
                    continuation=self.continuation(attempt),
                )
            if reconciliation.status != "not_executed":
                self.owner._compatibility.suspend_reconciliation()
        reason = await self.authorize(attempt)
        attempt = checked_record(
            self.store.get_attempt(self.thread_id, attempt.tool_call_id),
            kind="attempt",
            thread_id=self.thread_id,
            tool_call_id=attempt.tool_call_id,
            allow_none=True,
        )
        if reason:
            # Known no effect after reconciliation permits blocked settlement.
            if attempt.status == "executing":
                raise ConfigurationError("durable_reconciliation_required")
            message = result_message(
                attempt,
                invocation=self,
                continuation=self.continuation(attempt),
                content="Action blocked: " + reason + ". The tool was not executed.",
                status="error",
            )
            return tool_message(
                await self.settle(attempt, message, blocked=True, reason=reason),
                invocation=self,
                continuation=self.continuation(attempt),
            )
        if self.native_cancelled:
            raise ConfigurationError("durable_reconciliation_required")

        def reserve_execution():
            return self._reserve_execution(attempt)

        attempt = await self.storage_call(reserve_execution)
        if self.native_cancelled:
            raise ConfigurationError("durable_reconciliation_required")
        return await self._execute_reserved_call(
            request,
            handler,
            attempt=attempt,
            call=call,
            continuation=continuation,
            generated=generated,
        )

    async def _execute_read_call(
        self, request, handler, *, contract, deep, call_context, context_arguments
    ):
        entry = (
            self.owner.catalog.resolve(
                call_context.child_path, request.tool_call.get("name")
            )
            if deep
            else self.owner.catalog.entries.get(request.tool_call.get("name"))
        )
        if entry is None or not entry.matches(request.tool, request.tool_call["name"]):
            raise ConfigurationError("tool_binding_changed")
        readiness = contract.readiness
        if (
            readiness is None
            or not readiness.eligible
            or not readiness.policy_ready
            or readiness.reason != "read_only"
            or deep
            and readiness.governance_revision_id is None
        ):
            raise ConfigurationError("governance_not_ready")
        call, _, contract = self._validate_call(request, **context_arguments)

        def reserve_read():
            thread = checked_record(
                self.store.load(self.thread_id),
                kind="thread",
                thread_id=self.thread_id,
                allow_none=True,
            )
            if (
                thread.read_calls
                + len(
                    checked_record(
                        self.store.attempts(self.thread_id),
                        kind="attempt",
                        thread_id=self.thread_id,
                    )
                )
                >= self.owner.control.durable.max_tool_attempts
            ):
                raise ConfigurationError("durable_limit_exhausted")
            checked_record(
                self.store.transition(
                    self.thread_id,
                    thread.revision,
                    DurableTransition(read_call_increment=1),
                ),
                kind="thread",
                thread_id=self.thread_id,
            )

        await self.storage_call(reserve_read)
        with self.owner.lock, self.lock:
            self.owner._ensure_open()
            if self.native_cancelled:
                raise ConfigurationError("durable_reconciliation_required")
        token = CURRENT_READ_CONTRACT.set(contract)
        try:
            return await handler(copy_tool_call_request(request, call))
        finally:
            CURRENT_READ_CONTRACT.reset(token)

    def _replay_settled_attempt(self, attempt, *, continuation, generated):
        message = tool_message(attempt, invocation=self, continuation=continuation)
        if generated and attempt.status != "blocked":
            receipt = self.store.generated_state_receipt(
                self.thread_id, attempt.tool_call_id
            )
            replay = self.generated_replays.get(attempt.tool_call_id)
            if (
                receipt is None
                or replay is None
                or replay.disposition not in {"replayable", "already_committed"}
            ):
                raise ConfigurationError("durable_reconciliation_required")
            return self.owner._compatibility.generated_result(
                self.owner._compatibility.tool_result(message),
                invocation=self,
                continuation=continuation,
                receipt=receipt,
                replay=replay,
            )
        return message

    def _reserve_execution(self, attempt):
        if self.native_cancelled:
            raise ConfigurationError("durable_reconciliation_required")
        return checked_record(
            self.store.transition_attempt(
                self.thread_id,
                attempt.decision_id,
                attempt.revision,
                AttemptTransition(
                    "executing",
                    event=self.lifecycle_record(attempt, "resumed")
                    if attempt.status == "waiting"
                    else None,
                ),
            ),
            kind="attempt",
            thread_id=self.thread_id,
            decision_id=attempt.decision_id,
        )

    async def _execute_reserved_call(
        self, request, handler, *, attempt, call, continuation, generated
    ):
        effective = copy_tool_call_request(request, call)
        if attempt.idempotency_key:
            effective = effective.override(
                tool=FrozenInjectedTool(request.tool, attempt.native_call_json.encode())
            )
        correlation = _DurableToolCorrelation(self, attempt.event_id)
        token = CURRENT_TOOL_ATTEMPT.set(correlation)
        parent_token = None
        if generated and continuation.kind == "delegation":
            try:
                from openeight.deepagents._parents import (
                    CURRENT_PARENT_DELEGATION,
                    parent_delegation,
                )

                target = (
                    *json.loads(continuation.child_path_json),
                    call["args"].get("subagent_type"),
                )
                if not any(
                    node["path"] == json.loads(continuation.child_path_json)
                    and list(target) in node["delegates_to"]
                    for node in self.owner._composition["topology"]
                ):
                    raise ConfigurationError("invalid_input")
                parent_token = CURRENT_PARENT_DELEGATION.set(
                    parent_delegation(attempt, continuation, target_child_path=target)
                )
            except BaseException:
                CURRENT_TOOL_ATTEMPT.reset(token)
                raise
        try:
            with self.owner.lock, self.lock:
                self.owner._ensure_open()
                if self.native_cancelled:
                    raise ConfigurationError("durable_reconciliation_required")
                self.handlers += 1
        except BaseException:
            if parent_token is not None:
                CURRENT_PARENT_DELEGATION.reset(parent_token)
            CURRENT_TOOL_ATTEMPT.reset(token)
            raise
        try:
            try:
                execution_started_at, execution_clock = (
                    utc_timestamp(),
                    time.monotonic(),
                )
                captured = None
                if generated:
                    captured = await self.owner._compatibility.capture_generated(
                        effective,
                        handler,
                        invocation=self,
                        attempt=attempt,
                        continuation=continuation,
                    )
                    result = self.owner._compatibility.tool_message(captured.result)
                else:
                    result = await handler(effective)
            except Exception as error:
                if generated:
                    if self.owner._compatibility.is_interrupt(error):
                        raise
                    raise ConfigurationError(
                        "durable_reconciliation_required"
                    ) from None
                result = result_message(
                    attempt,
                    invocation=self,
                    continuation=self.continuation(attempt),
                    content="Tool failed",
                    status="error",
                )
            execution_latency_ms = max(
                0, int((time.monotonic() - execution_clock) * 1000)
            )
            normalized = self.owner._compatibility.tool_result(result)
            if normalized.status == "error" and not generated:
                if not attempt.idempotency_key:
                    raise ConfigurationError("durable_reconciliation_required")
                reconciliation = await self.owner.control.durable.reconcile(attempt)
                if reconciliation.status == "confirmed":
                    value = json.loads(reconciliation.result_json)
                    result = result_message(
                        attempt,
                        invocation=self,
                        continuation=self.continuation(attempt),
                        content=value["content"],
                        status=value["status"],
                    )
                elif reconciliation.status != "not_executed":
                    self.owner._compatibility.suspend_reconciliation()
            normalized = self.owner._compatibility.tool_result(result)
            if normalized.status == "error" and not generated:
                result = self.owner._compatibility.tool_message(
                    NativeToolResult(
                        normalized.tool_call_id,
                        normalized.tool_name,
                        "The tool failed without an effect.",
                        "error",
                    )
                )
            settled = await self.settle(
                attempt,
                result,
                execution_started_at=execution_started_at,
                execution_latency_ms=execution_latency_ms,
                **({"generated": captured} if captured is not None else {}),
            )
            if captured is not None:
                return captured.native_result
            return tool_message(settled, invocation=self, continuation=continuation)
        finally:
            if parent_token is not None:
                CURRENT_PARENT_DELEGATION.reset(parent_token)
            CURRENT_TOOL_ATTEMPT.reset(token)
            with self.lock:
                self.handlers -= 1
