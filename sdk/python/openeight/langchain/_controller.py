"""Private framework bridge to the shared attempt authorization machinery."""

import asyncio
import json
from contextvars import ContextVar
from types import SimpleNamespace

from openeight import ConfigurationError

from ._authorization import OpenEightMiddleware

CURRENT_BINDING = ContextVar("openeight_binding", default=None)


class AttemptController(OpenEightMiddleware):
    def __init__(self, governance, path=(), *, observed=False):
        super().__init__(
            agent_name=governance.agent_name,
            endpoint=governance.endpoint,
            credential=governance.credential,
            delivery=governance.delivery,
            on_block=governance.on_block,
            approval=governance.approval,
            durable=governance.durable,
        )
        self.child_path, self.observed = path, observed

    def wrap_tool_call(self, request, handler):
        raise ConfigurationError("unsupported_execution_method")

    async def awrap_tool_call(self, request, handler):
        invocation = self._require_invocation()
        binding = self._entry(invocation, request)
        if binding is not None and binding.remote is not None:
            native_handler = handler

            async def dispatch(final):
                from openeight.mcp._coordinator import CURRENT_PERMIT

                call = json.dumps(
                    final.tool_call,
                    separators=(",", ":"),
                    sort_keys=True,
                    allow_nan=False,
                ).encode()
                permit = self.owner.coordinator.permit(
                    binding.remote, invocation, self.child_path, self.observed, call
                )
                token = CURRENT_PERMIT.set(permit)
                try:
                    return await native_handler(final)
                finally:
                    CURRENT_PERMIT.reset(token)
                    with binding.remote.source.lock:
                        binding.remote.source.inflight -= 1

            handler = dispatch
        if not self.observed:

            async def governed_dispatch(final):
                token = CURRENT_BINDING.set(binding)
                try:
                    return await handler(final)
                finally:
                    CURRENT_BINDING.reset(token)

            if (
                getattr(invocation, "durable", False)
                and self.owner._integration == "deepagents"
            ):
                from openeight.deepagents._continuation import (
                    CURRENT_DURABLE_CALL,
                    capture_durable_call_context,
                )
                from openeight.deepagents._parents import (
                    CURRENT_PARENT_DELEGATION,
                    validate_parent_delegation,
                )

                native = self.owner._compatibility.native_identity(
                    request, invocation=invocation, child_path=self.child_path
                )
                parent_key = validate_parent_delegation(
                    CURRENT_PARENT_DELEGATION.get(),
                    invocation=invocation,
                    native=native,
                )
                context = capture_durable_call_context(
                    request,
                    invocation=invocation,
                    native=native,
                    parent_attempt_key=parent_key,
                )
                context_token = CURRENT_DURABLE_CALL.set(context)
                binding_token = CURRENT_BINDING.set(binding)
                try:
                    return await super().awrap_tool_call(request, governed_dispatch)
                finally:
                    CURRENT_BINDING.reset(binding_token)
                    CURRENT_DURABLE_CALL.reset(context_token)
            return await super().awrap_tool_call(request, governed_dispatch)
        invocation = self._require_invocation()
        binding = self._entry(invocation, request)
        if binding is None or not binding.matches(
            request.tool, request.tool_call["name"]
        ):
            raise ConfigurationError("tool_binding_changed")
        attempt = SimpleNamespace(invocation=invocation)
        from ._checkpoint_claim import assert_checkpoint_claim

        await assert_checkpoint_claim()
        self._claim_handler(attempt)

        async def execute():
            token = CURRENT_BINDING.set(binding)
            try:
                return await handler(request)
            finally:
                CURRENT_BINDING.reset(token)
                self._settle_handler(attempt)

        task = asyncio.create_task(execute())
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        return await asyncio.shield(task)
