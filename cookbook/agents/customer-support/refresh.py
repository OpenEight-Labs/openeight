"""Bound refresh to durable calls attributed to command messages, never error text."""

import json
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, ToolMessage

from local import LocalError

from retry_policy import REMEDIES, RemedyRetryPolicy


class RefreshMiddleware(AgentMiddleware):
    def __init__(self, store, retailer, thread_id):
        self.store, self.retailer, self.thread_id = store, retailer, thread_id
        self.messages = ()
        self.policy = RemedyRetryPolicy(store, retailer, thread_id)


    async def awrap_model_call(self, request, handler):
        self.messages = request.messages
        failed = [
            a
            for a in self.policy.attempts()
            if a.tool_name in REMEDIES and a.status == "failed"
        ]
        refreshed = None
        blocked = set()
        for attempt in failed:
            action_id = json.loads(attempt.native_call_json)["args"].get("action_id")
            try:
                evaluation = self.policy.evaluate(action_id, request.messages)
                decision, fresh = evaluation.state, evaluation.arguments
            except LocalError:
                decision, fresh = "blocked", None
            if decision == "refresh" and refreshed is None:
                refreshed = {
                    "id": str(uuid4()),
                    "name": attempt.tool_name,
                    "args": fresh,
                }
            elif decision in {"blocked", "completed"}:
                blocked.add(attempt.tool_name)
        # Let native SDK model admission/callbacks run, including recovery reset.
        response = await handler(
            request.override(tools=[t for t in request.tools if t.name not in blocked])
        )
        if refreshed:
            return ModelResponse(result=[AIMessage(content="", tool_calls=[refreshed])])
        return response

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        if call.get("name") in REMEDIES:
            attempts = self.policy.attempts()
            standalone = self.store.deployment.mode == "standalone"
            if standalone or not any(a.tool_call_id == call["id"] for a in attempts):
                try:
                    # Standalone journals this call before entering middleware;
                    # evaluate its retry budget against earlier calls only.
                    evaluation = self.policy.evaluate(
                        call["args"].get("action_id"), self.messages,
                        exclude_call_id=call["id"] if standalone else None,
                    )
                    decision, fresh = evaluation.state, evaluation.arguments
                    arguments = {
                        k: v for k, v in call["args"].items()
                        if k != "idempotency_key"
                    }
                    if decision in {"blocked", "completed"} or (
                        decision == "refresh" and fresh != arguments
                    ):
                        raise LocalError("refresh_blocked")
                except LocalError:
                    return ToolMessage(
                        content="This action requires explicit customer input or support review.",
                        status="error",
                        tool_call_id=call["id"],
                        name=call["name"],
                    )
        return await handler(request)
