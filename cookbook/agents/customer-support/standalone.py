"""Local graph execution with journaled actions and canonical receipt recovery."""

import asyncio
from copy import deepcopy
from dataclasses import asdict
import json
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from pydantic import PrivateAttr

from openeight.langchain.durable import CommandSettlement, DurableTransition

from local import LocalError, canonical, now
from retailer import KINDS, validate_arguments
from storage import LocalAttempt


def checkpoint(snapshot):
    return (
        snapshot.config.get("configurable", {}).get("checkpoint_id")
        if snapshot.config
        else None
    )


def visible(snapshot, command_id):
    messages = snapshot.values.get("messages", [])
    positions = [i for i, message in enumerate(messages) if message.id == command_id]
    if len(positions) != 1:
        raise LocalError("checkpoint_conflict")
    result = []
    for message in messages[positions[0] + 1 :]:
        if message.type == "human":
            break
        if message.type == "ai" and not message.tool_calls and message.text:
            if len(str(message.text).encode()) > 8192:
                raise LocalError("invalid_output")
            result.append(str(message.text))
    return tuple(result)


def result_message(attempt):
    value = json.loads(attempt.result_json)
    return ToolMessage(
        content=value["content"],
        status=value["status"],
        tool_call_id=attempt.tool_call_id,
        name=attempt.tool_name,
    )


class LocalToolProxy(BaseTool):
    _original: BaseTool = PrivateAttr()
    _call: dict = PrivateAttr()
    _consumed: bool = PrivateAttr(default=False)

    def __init__(self, original, call):
        super().__init__(
            name=original.name,
            description=original.description,
            args_schema=original.args_schema,
        )
        self._original, self._call = original, deepcopy(call)

    def _run(self, *args, **kwargs):
        raise LocalError("unsupported_execution_method")

    async def ainvoke(self, input, config=None, **kwargs):
        public = {
            **self._call,
            "args": {
                k: v for k, v in self._call["args"].items() if k != "idempotency_key"
            },
        }
        if canonical(input) != canonical(public) or self._consumed:
            raise LocalError("attempt_conflict")
        self._consumed = True
        return await self._original.ainvoke(self._call, config, **kwargs)


class LocalMiddleware(AgentMiddleware):
    def __init__(self, store, retailer, saver, thread_id):
        self.store, self.retailer, self.saver, self.thread_id = (
            store,
            retailer,
            saver,
            thread_id,
        )
        self.execution = asyncio.Lock()
        self.bound_tools = None
        self.recovering = False

    async def storage(self, function):
        async with self.saver.lock:
            return function()

    async def awrap_model_call(self, request, handler):
        thread = self.store.load(self.thread_id)
        if thread.model_calls >= 16:
            raise LocalError("model_limit_exhausted")
        await self.storage(
            lambda: self.store.transition(
                thread.id, thread.revision, DurableTransition(model_call_increment=1)
            )
        )
        self.recovering = False
        return await handler(request)

    async def awrap_tool_call(self, request, handler):
        async with self.execution:
            call = deepcopy(request.tool_call)
            if (
                self.bound_tools is None
                or self.bound_tools.get(call.get("name")) is not request.tool
            ):
                raise LocalError("unsupported_tool_definition")
            if (
                not isinstance(call.get("id"), str)
                or not call["id"]
                or len(call["id"].encode()) > 256
            ):
                raise LocalError("invalid_input")
            if "idempotency_key" in call.get("args", {}):
                raise LocalError("invalid_input")
            validate_arguments(call["name"], call.get("args"))
            schema = request.tool.tool_call_schema
            try:
                validated = schema.model_validate(
                    call["args"], strict=True
                ).model_dump()
                if canonical(validated) != canonical(call["args"]):
                    raise ValueError
            except Exception:
                raise LocalError("invalid_input") from None
            attempts = self.store.local_attempts(self.thread_id)
            previous = next((a for a in attempts if a.tool_call_id == call["id"]), None)
            write = call["name"] in KINDS
            if previous:
                if write:
                    call["args"]["idempotency_key"] = previous.idempotency_key
                if previous.native_call_json != canonical(call) or write != (
                    previous.idempotency_key is not None
                ):
                    raise LocalError("attempt_conflict")
                attempt = previous
            else:
                if self.recovering:
                    raise LocalError("attempt_missing")
                if len(attempts) >= 64:
                    raise LocalError("tool_limit_exhausted")
                key = str(uuid4()) if write else None
                if key:
                    call["args"]["idempotency_key"] = key
                ts = now()
                attempt = await self.storage(
                    lambda: self.store.local_prepare(
                        LocalAttempt(
                            self.store.deployment.id,
                            str(uuid4()),
                            self.thread_id,
                            call["id"],
                            call["name"],
                            canonical(call),
                            ts,
                            ts,
                            idempotency_key=key,
                        )
                    )
                )
            if attempt.status in {"succeeded", "failed"}:
                return result_message(attempt)
            if write and attempt.status in {"executing", "reconciliation_required"}:
                receipt = self.retailer.lookup_receipt(attempt.idempotency_key)
                if receipt is not None:
                    expected = canonical(
                        {
                            "tool": attempt.tool_name,
                            "context": asdict(self.retailer.context),
                            "arguments": {
                                k: v
                                for k, v in call["args"].items()
                                if k != "idempotency_key"
                            },
                        }
                    )
                    if (
                        receipt["request_json"] != expected
                        or receipt["kind"] != KINDS[attempt.tool_name]
                    ):
                        raise LocalError("receipt_conflict")
                    if attempt.status != "executing":
                        attempt = await self.storage(
                            lambda: self.store.local_update(
                                self.thread_id,
                                attempt.id,
                                attempt.revision,
                                "executing",
                            )
                        )
                    settled = await self.storage(
                        lambda: self.store.local_update(
                            self.thread_id,
                            attempt.id,
                            attempt.revision,
                            "succeeded",
                            canonical(
                                {"content": receipt["result_json"], "status": "success"}
                            ),
                        )
                    )
                    return result_message(settled)
            attempt = await self.storage(
                lambda: self.store.local_update(
                    self.thread_id, attempt.id, attempt.revision, "executing"
                )
            )
            effective = request.override(tool_call=call)
            if write:
                effective = effective.override(tool=LocalToolProxy(request.tool, call))
            try:
                result = await handler(effective)
            except LocalError:
                if not write or self.retailer.lookup_receipt(attempt.idempotency_key) is None:
                    result = ToolMessage(content="Support action failed without an effect.", status="error",
                                         tool_call_id=attempt.tool_call_id, name=attempt.tool_name)
                else:
                    # The normal error-result path below verifies the exact receipt.
                    result = ToolMessage(content="Reconcile committed effect.", status="error",
                                         tool_call_id=attempt.tool_call_id, name=attempt.tool_name)
            except Exception:
                raise LocalError("action_recovery_required") from None
            if (
                not isinstance(result, ToolMessage)
                or not isinstance(result.content, str)
                or len(result.content.encode()) > 65536
            ):
                raise LocalError("invalid_tool_result")
            if result.status == "error" and write:
                receipt = self.retailer.lookup_receipt(attempt.idempotency_key)
                if receipt is not None:
                    expected = canonical(
                        {
                            "tool": attempt.tool_name,
                            "context": asdict(self.retailer.context),
                            "arguments": {
                                k: v
                                for k, v in call["args"].items()
                                if k != "idempotency_key"
                            },
                        }
                    )
                    if (
                        receipt["request_json"] != expected
                        or receipt["kind"] != KINDS[attempt.tool_name]
                    ):
                        raise LocalError("receipt_conflict")
                    result = ToolMessage(
                        content=receipt["result_json"],
                        status="success",
                        tool_call_id=attempt.tool_call_id,
                        name=attempt.tool_name,
                    )
            if result.status == "error":
                result = ToolMessage(
                    content="Support action failed. No confirmed effect.",
                    status="error",
                    tool_call_id=attempt.tool_call_id,
                    name=attempt.tool_name,
                )
            attempt = await self.storage(
                lambda: self.store.local_update(
                    self.thread_id,
                    attempt.id,
                    attempt.revision,
                    "failed" if result.status == "error" else "succeeded",
                    canonical({"content": result.content, "status": result.status}),
                )
            )
            return result_message(attempt)


async def execute(store, retailer, graph, middleware, command_id):
    task = asyncio.create_task(_execute(store, retailer, graph, middleware, command_id))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
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


async def _execute(store, retailer, graph, middleware, command_id):
    with store.acquire(store.find_command(command_id).thread_id):
        command = store.find_command(command_id)
        thread = store.load(command.thread_id)
        if command.status != "accepted":
            return
        try:
            command = await middleware.storage(
                lambda: store.begin_command(
                    thread.id, command.id, command.revision, thread.revision
                )
            )
            execution = command
            seen = {execution.id}
            while execution.recover_command_id:
                execution = store.command(thread.id, execution.recover_command_id)
                if execution.id in seen:
                    raise LocalError("command_conflict")
                seen.add(execution.id)
            original = execution if execution.id != command.id else None
            config = {"configurable": {"thread_id": thread.id}, "recursion_limit": 128}
            snapshot = await graph.aget_state(config)
            saved_id = checkpoint(snapshot)
            if thread.checkpoint_id and not saved_id:
                raise LocalError("checkpoint_missing")
            request = json.loads(execution.request_json)
            present = any(
                m.id == execution.id for m in snapshot.values.get("messages", [])
            )
            middleware.recovering = command.kind == "resume"
            if execution.kind != "resume":
                if not execution.input_recorded:
                    if present:
                        raise LocalError("checkpoint_conflict")
                    execution = await middleware.storage(
                        lambda: store.record_command_input(
                            thread.id, execution.id, execution.revision, saved_id
                        )
                    )
                if not present:
                    if execution.input_checkpoint_id != saved_id:
                        raise LocalError("checkpoint_conflict")
                    await graph.ainvoke(
                        {
                            "messages": [
                                HumanMessage(
                                    content=request["message"], id=execution.id
                                )
                            ]
                        },
                        config,
                    )
                elif snapshot.next:
                    await graph.ainvoke(None, config)
            else:
                if not saved_id:
                    raise LocalError("checkpoint_missing")
                if not execution.input_recorded:
                    execution = await middleware.storage(
                        lambda: store.record_command_input(
                            thread.id, execution.id, execution.revision, saved_id
                        )
                    )
                if snapshot.next:
                    await graph.ainvoke(None, config)
            snapshot = await graph.aget_state(config)
            attempts = store.local_attempts(thread.id)
            if any(a.status not in {"succeeded", "failed"} for a in attempts):
                raise LocalError("action_recovery_required")
            if execution.kind != "resume":
                turns = visible(snapshot, execution.id)
            else:
                before = await graph.aget_state(
                    {
                        "configurable": {
                            "thread_id": thread.id,
                            "checkpoint_id": execution.input_checkpoint_id,
                        }
                    }
                )
                prior_ids = {m.id for m in before.values.get("messages", [])}
                turns = tuple(
                    str(m.text)
                    for m in snapshot.values.get("messages", [])
                    if m.id not in prior_ids
                    and m.type == "ai"
                    and not m.tool_calls
                    and m.text
                )
            disposition = "completed" if retailer.disposition() == "terminal" else "waiting_customer"
            thread = store.load(thread.id)
            command = store.command(thread.id, command.id)
            original = store.command(thread.id, original.id) if original else None
            await middleware.storage(
                lambda: store.settle_command(
                    thread.id,
                    command.id,
                    command.revision,
                    CommandSettlement(
                        "completed",
                        thread.revision,
                        disposition,
                        checkpoint(snapshot),
                        turns if not original else (),
                        recovered_command_id=original.id if original else None,
                        recovered_command_revision=original.revision
                        if original
                        else None,
                        recovered_status="completed" if original else None,
                        recovered_turns=turns if original else (),
                    ),
                )
            )
        except BaseException:
            thread = store.load(thread.id)
            if thread.status != "completed":
                await middleware.storage(
                    lambda: store.transition(
                        thread.id,
                        thread.revision,
                        DurableTransition(
                            status="reconciliation_required",
                            observation_incomplete=True,
                        ),
                    )
                )
            raise
