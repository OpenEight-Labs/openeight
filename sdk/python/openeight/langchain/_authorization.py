"""Construction-time authorization of each actual tool-handler attempt."""

import asyncio
import json
import os
import random
import threading
import time
from copy import deepcopy
from dataclasses import dataclass

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from openeight import (
    APIError,
    ApprovalTimeout,
    BlockedError,
    ConfigurationError,
    DecisionResult,
    Event,
    ProtocolError,
    TransportError,
)
from openeight.client import _ADAPTER_DEADLINE
from openeight.telemetry import uuid7

from ._callbacks import bounded_name
from ._compat import copy_tool_call_request
from ._content import tool_output
from ._deployment import checked_record
from ._runtime import (
    CURRENT_INVOCATION,
    CURRENT_READ_CONTRACT,
    CURRENT_TOOL_ATTEMPT,
    tool_parent_event,
    utc_timestamp,
)
from .configuration import ApprovalConfig


@dataclass(frozen=True)
class ExecutionContract:
    """Attempt-local requirements, separate from immutable publication receipts."""

    publication: object
    execution_readiness: object

    @property
    def tool(self):
        return self.publication.tool

    @property
    def tool_version(self):
        return self.publication.tool_version

    @property
    def requirements_bytes(self):
        from dataclasses import asdict

        return len(
            json.dumps(asdict(self.execution_readiness), ensure_ascii=False).encode()
        )


@dataclass
class GovernedToolAttempt:
    """Frozen input and identities for one Decision and handler execution."""

    invocation: object
    decision_id: str
    event_id: str
    source_time: str
    selected: bytes
    contract: object
    native_call: bytes
    started: float
    callback_claimed: bool = False
    parent: str | None = None
    waited: bool = False
    execution_started_at: str | None = None

    def request(self):
        """Rebuild identical Decide arguments for the initial call and approval replay."""
        return dict(
            decision_id=self.decision_id,
            run_id=self.invocation.run_id,
            tool_id=self.contract.tool.id,
            tool_version_id=self.contract.tool_version.id,
            source_time=self.source_time,
            **json.loads(self.selected),
        )


def _sanitize_sdk_errors(method):
    """Expose fixed SDK errors without retaining credential-bearing adapter frames."""
    from functools import wraps
    from inspect import iscoroutinefunction

    from openeight.errors import SDKError, _fresh_safe_error, _safe_error_descriptor

    if iscoroutinefunction(method):

        @wraps(method)
        async def asynchronous(self, request, handler):
            failure = None
            try:
                return await method(self, request, handler)
            except SDKError as error:
                failure = _safe_error_descriptor(error)
            self = request = handler = None
            raise _fresh_safe_error(failure) from None

        return asynchronous

    @wraps(method)
    def synchronous(self, request, handler):
        failure = None
        try:
            return method(self, request, handler)
        except SDKError as error:
            failure = _safe_error_descriptor(error)
        self = request = handler = None
        raise _fresh_safe_error(failure) from None

    return synchronous


class OpenEightMiddleware(AgentMiddleware):
    """Authorize actual tool attempts in a supported native create_agent graph."""

    def __init__(
        self,
        *,
        agent_name,
        endpoint,
        credential,
        delivery=None,
        on_block="message",
        approval=None,
        durable=None,
    ):
        if (
            on_block not in ("message", "raise")
            or approval is not None
            and not isinstance(approval, ApprovalConfig)
        ):
            raise ConfigurationError("invalid_integration_configuration")
        from .durable import DurableApprovalConfig

        if durable is not None and (
            not isinstance(durable, DurableApprovalConfig) or approval is not None
        ):
            raise ConfigurationError("invalid_integration_configuration")
        self.durable = durable
        # Validate mandatory configuration locally without opening a socket.
        from openeight.client import (
            _validate_agent_name,
            _validate_api_key,
            _validate_base_url,
        )

        _validate_agent_name(agent_name)
        _validate_base_url(endpoint)
        _validate_api_key(credential)
        self._binding = dict(
            integration="langchain",
            agent_name=agent_name,
            endpoint=endpoint,
            credential=credential,
            delivery=delivery,
        )
        self.approval = approval or ApprovalConfig()
        self.on_block = on_block
        self.owner = None
        self._binding_lock = threading.RLock()
        self._binding_pid = os.getpid()

    def instrument(self, agent):
        from ._owner import InstrumentedAgent

        # An unbound middleware has no transport; its owner may be created after fork.
        if self.owner is not None:
            self.owner._ensure_open()
        elif self._binding_pid != os.getpid():
            self._binding_lock = threading.RLock()
            self._binding_pid = os.getpid()
        with self._binding_lock:
            if self.owner is not None:
                if agent is self.owner or agent is self.owner._graph:
                    self.owner._ensure_open()
                    return self.owner
                raise ConfigurationError("invalid_integration_configuration")
            self.owner = InstrumentedAgent(agent, control=self, **self._binding)
            return self.owner

    def before_agent(self, state, runtime):
        self._require_invocation()

    async def abefore_agent(self, state, runtime):
        self._require_invocation()

    def wrap_model_call(self, request, handler):
        self._validate_model(request)
        if self.durable is not None:
            raise ConfigurationError("unsupported_execution_method")
        return handler(request)

    async def awrap_model_call(self, request, handler):
        self._validate_model(request)
        from ._checkpoint_claim import assert_checkpoint_claim

        await assert_checkpoint_claim()
        if self.durable is not None:
            invocation = self._require_invocation()
            invocation.recovering = False
            if getattr(invocation, "native_cancelled", False):
                raise ConfigurationError("durable_reconciliation_required")
            from .durable import DurableTransition

            def reserve_model():
                if getattr(invocation, "native_cancelled", False):
                    raise ConfigurationError("durable_reconciliation_required")
                thread = checked_record(
                    self.durable.store.load(invocation.thread_id),
                    kind="thread",
                    thread_id=invocation.thread_id,
                    allow_none=True,
                )
                if thread.model_calls >= self.durable.max_model_calls:
                    raise ConfigurationError("durable_limit_exhausted")
                checked_record(
                    self.durable.store.transition(
                        thread.id,
                        thread.revision,
                        DurableTransition(model_call_increment=1),
                    ),
                    kind="thread",
                    thread_id=thread.id,
                )

            await invocation.storage_call(reserve_model)
            with self.owner.lock, invocation.lock:
                self.owner._ensure_open()
                if getattr(invocation, "native_cancelled", False):
                    raise ConfigurationError("durable_reconciliation_required")
        return await handler(request)

    def _validate_model(self, request):
        expected = getattr(self, "_managed_model", None)
        if expected is not None and request.model is not expected:
            raise ConfigurationError("unsupported_execution_method")

    def _require_invocation(self):
        invocation = CURRENT_INVOCATION.get()
        if (
            self.owner is None
            or invocation is None
            or invocation.owner is not self.owner
            or invocation.root_ended
        ):
            raise ConfigurationError("integration_not_entered") from None
        self.owner._ensure_open()
        return invocation

    def _entry(self, invocation, request):
        catalog = invocation.catalog
        if hasattr(catalog, "resolve"):
            return catalog.resolve(self.child_path, request.tool_call["name"])
        return catalog.entries.get(request.tool_call["name"])

    def _prepare_tool_attempt(self, request, contract=None):
        """Freeze the selected input and native tool call before requesting a Decision."""
        invocation = self._require_invocation()
        try:
            call = deepcopy(request.tool_call)
            if (
                type(call) is not dict
                or type(call.get("id")) is not str
                or not call["id"]
                or len(call["id"].encode()) > 256
                or type(call.get("name")) is not str
            ):
                raise ValueError
            name = call["name"]
            entry = self._entry(invocation, request)
            if entry is None or not entry.matches(request.tool, name):
                raise ConfigurationError("unsupported_tool_definition")
            if (
                contract is None
                or not contract.tool.governed
                or contract.tool_version.tool_contract_digest is None
            ):
                raise ConfigurationError("governance_unconfigured")
            from openeight._json import decision_envelope
            from openeight.tools import collect_selected_values

            readiness = contract.execution_readiness
            if (
                not readiness.eligible
                or not readiness.policy_ready
                or readiness.policy_contract_set_digest is None
            ):
                raise ConfigurationError("governance_not_ready")
            selected = decision_envelope(
                {},
                readiness.policy_contract_set_digest,
                collect_selected_values(call["args"], readiness),
            )
            native = json.dumps(
                call, separators=(",", ":"), sort_keys=True, allow_nan=False
            ).encode()
        except ConfigurationError:
            raise
        except Exception:
            raise ConfigurationError("invalid_input") from None
        size = len(selected) + 2 * len(native) + contract.requirements_bytes + 512
        with self.owner.lock:
            if self.owner.attempts >= 1000 or not self.owner.worker.reserve(size):
                raise ConfigurationError("execution_busy")
            self.owner.attempts += 1
        attempt = GovernedToolAttempt(
            invocation,
            uuid7(),
            uuid7(),
            utc_timestamp(),
            selected,
            contract,
            native,
            time.monotonic(),
            parent=tool_parent_event(invocation, request),
        )
        return copy_tool_call_request(request, call), attempt, size

    def _validate_tool_attempt_binding(self, request, attempt):
        """Reject tool identity, schema or call changes after authorization."""
        entry = self._entry(attempt.invocation, request)
        try:
            native = json.dumps(
                request.tool_call,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode()
            valid = (
                entry is not None
                and entry.matches(request.tool, request.tool_call["name"])
                and native == attempt.native_call
            )
        except Exception:
            valid = False
        if not valid:
            raise ConfigurationError("unsupported_tool_definition") from None

    def _request_decision(self, attempt, deadline=None):
        if deadline is not None and time.monotonic() >= deadline:
            raise TransportError() from None
        token = _ADAPTER_DEADLINE.set(deadline) if deadline is not None else None
        try:
            result = self.owner.client.decide(**attempt.request())
        except APIError as error:
            if error.code in {
                "EXECUTION_BLOCKED",
                "AUTHORIZATION_STALE",
                "policy_input_contract_stale",
            }:
                return None, error.code
            raise
        finally:
            if token is not None:
                _ADAPTER_DEADLINE.reset(token)
        if (
            not isinstance(result, DecisionResult)
            or result.decision_id != attempt.decision_id
            or result.run_id != attempt.invocation.run_id
            or result.verdict
            not in {"allow", "deny", "approval_required", "dry_run_only"}
        ):
            raise ProtocolError() from None
        return result, None

    def _poll_approval(self, result, remaining):
        try:
            state = self.owner.client.approval_state(
                result.decision_id, timeout=remaining
            )
        except TransportError:
            return "pending"
        except APIError as error:
            if error.status_code == 429 or error.status_code >= 500:
                return "pending"
            raise
        if state not in {"pending", "approved", "rejected", "expired"}:
            raise ProtocolError() from None
        return state

    def _authorize_tool_attempt(self, attempt):
        result, block = self._request_decision(attempt)
        if block or not result.needs_approval:
            return result, block or (None if result.allowed else result.verdict)
        self._enqueue_lifecycle(attempt, "paused")
        attempt.waited = True
        deadline = time.monotonic() + self.approval.timeout
        interval = self.approval.poll_interval
        while time.monotonic() < deadline:
            state = self._poll_approval(result, max(0.001, deadline - time.monotonic()))
            if time.monotonic() >= deadline:
                break
            if state == "approved":
                replay, block = self._request_decision(attempt, deadline=deadline)
                return replay, block or (
                    None
                    if replay.allowed or replay.needs_approval and replay.replayed
                    else replay.verdict
                )
            if state != "pending":
                return result, "approval_" + state
            time.sleep(
                min(
                    deadline - time.monotonic(),
                    interval
                    * random.uniform(
                        1 - self.approval.poll_jitter, 1 + self.approval.poll_jitter
                    ),
                )
            )
            interval = min(
                self.approval.poll_max_interval,
                interval * self.approval.poll_backoff_multiplier,
            )
        return result, "approval_wait_timed_out"

    async def _authorize_tool_attempt_async(self, attempt):
        result, block = await self.owner._run_client_call(
            lambda: self._request_decision(attempt)
        )
        if block or not result.needs_approval:
            return result, block or (None if result.allowed else result.verdict)
        self._enqueue_lifecycle(attempt, "paused")
        attempt.waited = True
        deadline = time.monotonic() + self.approval.timeout
        interval = self.approval.poll_interval
        while time.monotonic() < deadline:
            try:
                state = await self.owner._run_client_call(
                    lambda: self._poll_approval(
                        result, max(0.001, deadline - time.monotonic())
                    ),
                    deadline=deadline,
                )
            except TransportError:
                if time.monotonic() >= deadline:
                    break
                raise
            if time.monotonic() >= deadline:
                break
            if state == "approved":
                replay, block = await self.owner._run_client_call(
                    lambda: self._request_decision(attempt), deadline=deadline
                )
                return replay, block or (
                    None
                    if replay.allowed or replay.needs_approval and replay.replayed
                    else replay.verdict
                )
            if state != "pending":
                return result, "approval_" + state
            await asyncio.sleep(
                max(
                    0,
                    min(
                        deadline - time.monotonic(),
                        interval
                        * random.uniform(
                            1 - self.approval.poll_jitter, 1 + self.approval.poll_jitter
                        ),
                    ),
                )
            )
            interval = min(
                self.approval.poll_max_interval,
                interval * self.approval.poll_backoff_multiplier,
            )
        return result, "approval_wait_timed_out"

    def _block(self, request, result, reason):
        if self.on_block == "raise":
            if reason == "approval_wait_timed_out":
                raise ApprovalTimeout(result) from None
            raise BlockedError(result, reason) from None
        return ToolMessage(
            content="Action blocked by policy. The tool was not executed.",
            tool_call_id=request.tool_call["id"],
            name=request.tool_call["name"],
            status="error",
            artifact=None,
        )

    def _release_attempt(self, attempt, size):
        with self.owner.lock:
            self.owner.attempts -= 1
        self.owner.worker.release(size)

    def _claim_handler(self, attempt):
        """Claim execution while holding the same owner lock that seals shutdown."""
        with self.owner.lock:
            self.owner._ensure_open()
            if attempt.invocation.root_ended:
                raise ConfigurationError("integration_closed")
            attempt.invocation.handlers += 1

    def _start_tool_execution(self, attempt):
        if attempt.waited:
            self._enqueue_lifecycle(attempt, "resumed")
        attempt.execution_started_at = utc_timestamp()
        attempt.started = time.monotonic()

    def _settle_handler(self, attempt):
        """Release handler ownership and attempt completion under one invocation lock."""
        invocation = attempt.invocation
        with invocation.lock:
            invocation.handlers -= 1
            if invocation.root_ended:
                invocation.ended = utc_timestamp()
        invocation.complete_when_settled()

    def _enqueue_lifecycle(self, attempt, transition):
        attempt.invocation.enqueue_event(
            uuid7(),
            Event(
                bounded_name(attempt.contract.tool.name, suffix="." + transition),
                "lifecycle",
                "unspecified",
                utc_timestamp(),
                "interrupted" if transition == "paused" else "unspecified",
                parent_event_id=attempt.parent,
                decision_id=attempt.decision_id,
                tool_id=attempt.contract.tool.id,
                tool_version_id=attempt.contract.tool_version.id,
                tool_name=attempt.contract.tool.name,
            ),
        )

    def _enqueue_tool_event(self, attempt, failure, result=None):
        latency_ms = max(0, int((time.monotonic() - attempt.started) * 1000))
        failure = (
            failure or isinstance(result, ToolMessage) and result.status == "error"
        )
        invocation = attempt.invocation
        invocation.enqueue_event(
            attempt.event_id,
            Event(
                bounded_name(attempt.contract.tool.name),
                "tool_call",
                invocation.operation,
                attempt.execution_started_at,
                "error" if failure else "success",
                parent_event_id=attempt.parent,
                decision_id=attempt.decision_id,
                input=json.loads(attempt.selected),
                output=None
                if failure
                else attempt.invocation.capture_content(
                    "tool_call", "output", lambda: tool_output(result)
                ),
                error={"kind": "tool_error"} if failure else None,
                tool_id=attempt.contract.tool.id,
                tool_version_id=attempt.contract.tool_version.id,
                tool_name=attempt.contract.tool.name,
                latency_ms=latency_ms,
            ),
        )

    def _fresh_contract(self, request):
        invocation = self._require_invocation()
        entry = self._entry(invocation, request)
        if entry is None or not entry.matches(request.tool, request.tool_call["name"]):
            raise ConfigurationError("tool_binding_changed")
        from ._catalog import report_catalog

        contracts = report_catalog(
            self.owner.catalog_delivery.publisher, (entry.facts,)
        )
        if len(contracts) != 1:
            raise ConfigurationError("tool_binding_changed")
        contract = contracts[0]
        with invocation.lock:
            invocation.contracts[entry.facts.name] = contract
        if contract.tool.capability not in {"read", "write"}:
            raise ConfigurationError("classification_required")
        if contract.tool.capability == "read":
            readiness = contract.readiness
            if (
                readiness is None
                or not readiness.eligible
                or not readiness.policy_ready
                or readiness.reason != "read_only"
            ):
                raise ConfigurationError("governance_not_ready")
        if contract.tool.capability == "write":
            readiness = self.owner.client.get_tool_readiness([contract.tool_version.id])
            if len(readiness.tools) != 1:
                raise ProtocolError()
            item = readiness.tools[0]
            if (
                item.tool_id != contract.tool.id
                or item.tool_version_id != contract.tool_version.id
            ):
                raise ProtocolError()
            return ExecutionContract(contract, item)
        return contract

    def _claim_read(self, request):
        invocation = self._require_invocation()
        entry = self._entry(invocation, request)
        if entry is None or not entry.matches(request.tool, request.tool_call["name"]):
            raise ConfigurationError("tool_binding_changed")
        with self.owner.lock:
            self.owner._ensure_open()
            if invocation.root_ended or self.owner.attempts >= 1000:
                raise ConfigurationError("execution_busy")
            if not self.owner.worker.reserve(512):
                raise ConfigurationError("execution_busy")
            self.owner.attempts += 1
            invocation.handlers += 1
        return invocation

    def _release_read(self, invocation):
        with self.owner.lock:
            self.owner.attempts -= 1
        self.owner.worker.release(512)
        with invocation.lock:
            invocation.handlers -= 1
        invocation.complete_when_settled()

    async def _execute_read_async(self, request, handler, contract):
        invocation = self._claim_read(request)

        async def execute():
            token = CURRENT_READ_CONTRACT.set(contract)
            try:
                return await handler(request)
            finally:
                CURRENT_READ_CONTRACT.reset(token)
                self._release_read(invocation)

        task = asyncio.create_task(execute())
        task.add_done_callback(
            lambda task: task.exception() if not task.cancelled() else None
        )
        return await asyncio.shield(task)

    @_sanitize_sdk_errors
    def wrap_tool_call(self, request, handler):
        if self.durable is not None:
            raise ConfigurationError("unsupported_execution_method")
        request = copy_tool_call_request(request, deepcopy(request.tool_call))
        contract = self._fresh_contract(request)
        if contract.tool.capability == "read":
            invocation = self._claim_read(request)
            token = CURRENT_READ_CONTRACT.set(contract)
            try:
                return handler(request)
            finally:
                CURRENT_READ_CONTRACT.reset(token)
                self._release_read(invocation)
        request, attempt, size = self._prepare_tool_attempt(request, contract)
        try:
            result, reason = self._authorize_tool_attempt(attempt)
            if reason:
                return self._block(request, result, reason)
            self._validate_tool_attempt_binding(request, attempt)
            self._claim_handler(attempt)
            token = CURRENT_TOOL_ATTEMPT.set(attempt)
            try:
                self._start_tool_execution(attempt)
                result = handler(request)
            except BaseException:
                self._enqueue_tool_event(attempt, True)
                raise
            else:
                self._enqueue_tool_event(attempt, False, result)
                return result
            finally:
                CURRENT_TOOL_ATTEMPT.reset(token)
                self._settle_handler(attempt)
        finally:
            self._release_attempt(attempt, size)

    @_sanitize_sdk_errors
    async def awrap_tool_call(self, request, handler):
        from ._checkpoint_claim import assert_checkpoint_claim

        await assert_checkpoint_claim()
        if self.durable is not None:
            invocation = self._require_invocation()
            if self.owner._integration == "deepagents":
                from openeight.deepagents._continuation import CURRENT_DURABLE_CALL

                return await invocation.tool_call(
                    request, handler, call_context=CURRENT_DURABLE_CALL.get()
                )
            return await invocation.tool_call(request, handler)
        request = copy_tool_call_request(request, deepcopy(request.tool_call))
        contract = await self.owner._run_client_call(
            lambda: self._fresh_contract(request)
        )
        if contract.tool.capability == "read":
            await assert_checkpoint_claim()
            return await self._execute_read_async(request, handler, contract)
        try:
            request, attempt, size = self._prepare_tool_attempt(request, contract)
        except ConfigurationError as error:
            if error.reason not in {"governance_unconfigured", "governance_not_ready"}:
                raise
            return self._block(request, None, error.reason)
        transferred = False
        try:
            result, reason = await self._authorize_tool_attempt_async(attempt)
            if reason:
                return self._block(request, result, reason)
            self._validate_tool_attempt_binding(request, attempt)
            await assert_checkpoint_claim()
            self._claim_handler(attempt)

            async def execute():
                token = CURRENT_TOOL_ATTEMPT.set(attempt)
                failure = False
                result = None
                try:
                    self._start_tool_execution(attempt)
                    result = await handler(request)
                    return result
                except BaseException:
                    failure = True
                    raise
                finally:
                    CURRENT_TOOL_ATTEMPT.reset(token)
                    try:
                        self._enqueue_tool_event(attempt, failure, result)
                    finally:
                        self._release_attempt(attempt, size)
                        self._settle_handler(attempt)

            task = asyncio.create_task(execute())
            task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
            transferred = True
            return await asyncio.shield(task)
        finally:
            if not transferred:
                self._release_attempt(attempt, size)
