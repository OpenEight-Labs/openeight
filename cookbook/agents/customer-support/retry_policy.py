"""Framework-independent remedy retry eligibility and command attribution."""

from dataclasses import dataclass
import json
from typing import Literal
from retailer import KINDS

REMEDIES = {
    name for name, kind in KINDS.items()
    if kind in {"refund", "replacement", "return", "cancellation"}
}


@dataclass(frozen=True)
class RetryEvaluation:
    state: Literal["fresh", "refresh", "completed", "blocked"]
    reason: str
    arguments: dict | None = None


class RemedyRetryPolicy:
    def __init__(self, store, retailer, thread_id):
        self.store, self.retailer, self.thread_id = store, retailer, thread_id

    def attempts(self):
        return (
            self.store.attempts(self.thread_id)
            if self.store.deployment.mode == "integrated"
            else self.store.local_attempts(self.thread_id)
        )

    def origins(self, messages):
        result, origin = {}, None
        for message in messages:
            if message.type == "human":
                command = self.store.find_command(message.id) if message.id else None
                origin = (
                    command.id
                    if command
                    and command.thread_id == self.thread_id
                    and command.kind in ("start", "reply")
                    else None
                )
            elif message.type == "ai":
                for call in message.tool_calls:
                    call_id = call["id"]
                    if call_id in result:
                        result[call_id] = None
                    else:
                        result[call_id] = origin
        return result, origin

    def action_attempts(self, action_id):
        return [
            a
            for a in self.attempts()
            if a.tool_name in REMEDIES
            and json.loads(a.native_call_json)["args"].get("action_id") == action_id
        ]

    def evaluate(self, action_id, messages, *, exclude_call_id=None):
        origins, current = self.origins(messages)
        attempts = [
            a for a in self.action_attempts(action_id)
            if a.tool_call_id != exclude_call_id
        ]
        if not current or any(origins.get(a.tool_call_id) is None for a in attempts):
            return RetryEvaluation("blocked", "command_origin_unknown")
        attempts = [a for a in attempts if origins[a.tool_call_id] == current]
        if not attempts:
            return RetryEvaluation("fresh", "no_attempt_for_command")
        if any(
            a.status
            in {
                "prepared",
                "executing",
                "waiting",
                "reconciliation_required",
                "blocked",
            }
            for a in attempts
        ):
            return RetryEvaluation("blocked", "attempt_unsettled")
        if any(a.status == "succeeded" for a in attempts):
            return RetryEvaluation("completed", "attempt_succeeded")
        if len(attempts) >= 2:
            return RetryEvaluation("blocked", "refresh_limit_reached")
        prior = attempts[0]
        if (
            prior.status != "failed"
            or self.retailer.lookup_receipt(prior.idempotency_key) is not None
        ):
            return RetryEvaluation("blocked", "no_confirmed_no_effect_failure")
        args = json.loads(prior.native_call_json)["args"]
        args.pop("idempotency_key", None)
        status = self.retailer.read(
            "get_resolution_status", {"case_id": self.retailer.context.case_id}
        )
        action = next((a for a in status["actions"] if a["id"] == action_id), None)
        if (
            not action
            or action["status"] != "confirmed"
            or status["case_status"] in ("handed_off", "resolved")
        ):
            return RetryEvaluation("blocked", "action_not_executable")
        if status["allocated_action_id"] not in (None, action["parent_action_id"]):
            return RetryEvaluation("blocked", "order_allocated")
        fresh = self.retailer.execution_arguments(action_id)
        if action["kind"] == "refund" and (
            fresh["amount"] != status["settlement_amount"]
            or fresh["currency"] != status["currency"]
        ):
            return RetryEvaluation("blocked", "balance_mismatch")
        return (
            RetryEvaluation("refresh", "facts_changed", fresh)
            if fresh != args else RetryEvaluation("blocked", "facts_unchanged")
        )
