"""Apply native Skills resolution and HITL edits before the final shared gate."""

from contextvars import ContextVar
from typing import Annotated, NotRequired

from deepagents.middleware.skills import SkillsMiddleware, SkillsState
from langchain.agents.middleware import AgentMiddleware, HumanInTheLoopMiddleware
from langchain.agents.middleware.types import PrivateStateAttr

from openeight import ConfigurationError
from openeight._framework._measurement import payload_hash

_REVIEWS = ContextVar("openeight_native_reviews", default=None)
_REVIEW_KEY = "openeight_native_reviewed_calls"


class ModelSelectionGuard(AgentMiddleware):
    """Keep the final provider call on the prepared node model."""

    def __init__(self, controller):
        self.controller = controller

    def wrap_model_call(self, request, handler):
        self.controller._validate_model(request)
        return handler(request)

    async def awrap_model_call(self, request, handler):
        from openeight.langchain._checkpoint_claim import assert_checkpoint_claim

        await assert_checkpoint_claim()
        self.controller._validate_model(request)
        return await handler(request)


class _State(SkillsState, HumanInTheLoopMiddleware.state_schema):
    openeight_native_reviewed_calls: NotRequired[
        Annotated[dict[str, str], PrivateStateAttr]
    ]


class _NativeApprovals(HumanInTheLoopMiddleware):
    def _create_action_and_config(self, tool_call, config, state, runtime):
        reviews = _REVIEWS.get()
        if reviews is not None:
            reviews[tool_call["id"]] = {
                "name": tool_call["name"],
                "args": tool_call["args"],
            }
        return super()._create_action_and_config(tool_call, config, state, runtime)


class GovernanceBoundary(SkillsMiddleware):
    state_schema = _State

    @property
    def name(self):
        return "SkillsMiddleware"

    def __init__(self, controller, *, sources=(), backend=None, interrupt_on=None):
        super().__init__(backend=backend, sources=list(sources or ()))
        self.controller = controller
        self._approvals = _NativeApprovals(interrupt_on=interrupt_on or {})

    def before_agent(self, state, runtime, config):
        self.controller._require_invocation()
        return super().before_agent(state, runtime, config)

    async def abefore_agent(self, state, runtime, config):
        from openeight.langchain._checkpoint_claim import assert_checkpoint_claim

        await assert_checkpoint_claim()
        self.controller._require_invocation()
        return await super().abefore_agent(state, runtime, config)

    def after_model(self, state, runtime):
        reviews = {}
        token = _REVIEWS.set(reviews)
        try:
            result = self._approvals.after_model(state, runtime)
        finally:
            _REVIEWS.reset(token)
        result = dict(result or {})
        edits = result.get("hitl_edited_tool_calls", {})
        result[_REVIEW_KEY] = {
            identity: payload_hash(edits.get(identity, action))
            for identity, action in reviews.items()
        }
        return result

    async def aafter_model(self, state, runtime):
        return self.after_model(state, runtime)

    def wrap_model_call(self, request, handler):
        def selected(final_request):
            return self.controller.wrap_model_call(final_request, handler)

        return super().wrap_model_call(request, selected)

    async def awrap_model_call(self, request, handler):
        async def selected(final_request):
            return await self.controller.awrap_model_call(final_request, handler)

        return await super().awrap_model_call(request, selected)

    def wrap_tool_call(self, request, handler):
        raise ConfigurationError("unsupported_execution_method")

    async def awrap_tool_call(self, request, handler):
        async def resolved(tool_request):
            async def edited(final_request):
                setting = self._approvals.interrupt_on.get(
                    final_request.tool_call["name"]
                )
                if setting is not None and (
                    setting.get("when") is None or setting["when"](final_request)
                ):
                    action = {
                        "name": final_request.tool_call["name"],
                        "args": final_request.tool_call["args"],
                    }
                    expected = final_request.state.get(_REVIEW_KEY, {}).get(
                        final_request.tool_call["id"]
                    )
                    if expected != payload_hash(action):
                        raise ConfigurationError("checkpoint_resume_invalid")
                return await self.controller.awrap_tool_call(final_request, handler)

            return await self._approvals.awrap_tool_call(tool_request, edited)

        return await super().awrap_tool_call(request, resolved)
