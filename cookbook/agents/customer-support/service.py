"""CLI command admission, execution and safe persisted projections."""

import asyncio
import json
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from openeight import SDKError

from agent import build_model, build_integrated_agent, build_standalone_agent
from configuration import load_binding, resolve_console_origin
from presentation import snapshot
from tools import DESCRIPTIONS
from local import LocalError
from retailer import RetailerRepository
from storage import SQLiteExecutionStore


class DemoService:
    def __init__(self, state, *, model_factory=None, console_origin=None):
        self.state = Path(state)
        self.deployment = load_binding(self.state)
        self.store = SQLiteExecutionStore(self.state, self.deployment)
        self.model_factory = model_factory
        self.console_origin = resolve_console_origin(self.deployment, console_origin)
        self.integration_status = (
            "standalone" if self.deployment.mode == "standalone" else "not_checked"
        )

    def retailer(self, case_id):
        return RetailerRepository.bind(self.state, self.deployment, case_id)

    def accept(self, command):
        load_binding(self.state, runtime=True)
        case = None
        if command.get("kind") == "start":
            case = self.retailer(command.get("case_id")).context.case_id
        accepted, _ = self.store.admit(command, case_id=case)
        return {
            "command_id": accepted.id,
            "thread_id": accepted.thread_id,
            "status": accepted.status,
        }

    def control(self, envelope):
        load_binding(self.state, runtime=True)
        if type(envelope) is not dict or "thread_id" not in envelope:
            raise LocalError("invalid_input")
        with self.store.acquire(envelope["thread_id"]):
            thread = self.store.load(envelope["thread_id"])
            if thread is None or thread.case_id != envelope.get("case_id"):
                raise LocalError("not_found")
            if thread.active_command_id or any(
                c.thread_id == thread.id for c in self.store.commands_for_dispatch()
            ):
                raise LocalError("execution_busy")
            retailer = self.retailer(thread.case_id)
            result = retailer.control(envelope)
            return {**result, "continuation": "saved; continue conversation"}

    def continue_conversation(self, thread_id, command_id):
        thread = self.store.load(thread_id)
        if thread is None:
            raise LocalError("not_found")
        previous = self.store.find_command(command_id)
        if previous:
            request = json.loads(previous.request_json)
            if (
                request.get("thread_id") != thread_id
                or request.get("kind") not in ("reply", "resume")
                or (
                    request["kind"] == "reply"
                    and request.get("message")
                    != "Continue using the saved retailer state."
                )
            ):
                raise LocalError("command_conflict")
            return self.accept(request)
        if thread.status == "waiting_customer" and not thread.active_command_id:
            return self.accept(
                {
                    "kind": "reply",
                    "command_id": command_id,
                    "thread_id": thread_id,
                    "message": "Continue using the saved retailer state.",
                }
            )
        return self.accept(
            {"kind": "resume", "command_id": command_id, "thread_id": thread_id}
        )

    def receipts(self, thread_id):
        thread = self.store.load(thread_id)
        if thread is None:
            raise LocalError("not_found")
        return tuple(
            {
                "receipt_id": r["id"],
                "kind": r["kind"],
                "created_at": r["created_at"],
                "result": json.loads(r["result_json"]),
            }
            for r in self.retailer(thread.case_id).receipts()
        )


    def snapshot(self, thread_id):
        return snapshot(
            self.store, self.retailer, thread_id, self.integration_status,
            self.console_origin, self.receipts,
        )

    def _model(self):
        return build_model(self.model_factory)

    async def _record_catalog(self, agent):
        status = await agent.aflush(1.0)
        snapshot = agent.catalog_snapshot()
        if (
            status.catalog_delivery_state != "acknowledged"
            or {fact.name for fact in snapshot.facts} != set(DESCRIPTIONS)
            or len(snapshot.results) != len(snapshot.facts)
            or any(
                result.status not in {"registered", "replayed"}
                or result.tool is None
                or result.tool_version is None
                or result.tool.name != snapshot.facts[result.request_index].name
                for result in snapshot.results
            )
        ):
            # A publication projection cannot distinguish rejected authority from
            # delivery failure. Preserve authenticated API errors for recovery.
            await agent.areadiness()
            raise LocalError("tool_catalog_unavailable")
        await asyncio.to_thread(
            self.store.record_catalog, snapshot.facts, snapshot.results
        )
        self.integration_status = (
            "ready"
            if all(
                (
                    (
                        result.tool.governed
                        and result.tool_version.tool_contract_digest is not None
                    )
                    or (
                        result.tool.capability == "read"
                        and not result.tool.governed
                        and result.readiness
                        and result.readiness.reason == "read_only"
                    )
                )
                and result.readiness
                and result.readiness.eligible
                and result.readiness.policy_ready
                for result in snapshot.results
            )
            else "awaiting_governance"
        )

    async def execute(self, command_id):
        try:
            return await self._execute(command_id)
        except SDKError:
            self.integration_status = "unavailable"
            if self.deployment.mode == "integrated" and self.store.unavailable(
                command_id
            ):
                return self.snapshot(self.store.find_command(command_id).thread_id)
            self.store.refuse_dispatch(command_id)
            raise
        except LocalError as error:
            if error.code in {
                "tool_schema_changed",
                "tool_catalog_conflict",
                "tool_catalog_unavailable",
            }:
                self.integration_status = "catalog_blocked"
                if self.store.unavailable(command_id):
                    return self.snapshot(self.store.find_command(command_id).thread_id)
            self.store.refuse_dispatch(command_id)
            raise
        except Exception:
            self.store.refuse_dispatch(command_id)
            raise

    async def _execute(self, command_id):
        d, key = load_binding(self.state, runtime=True)
        command = self.store.find_command(command_id)
        if command is None:
            raise LocalError("not_found")
        if command.status != "accepted":
            return self.snapshot(command.thread_id)
        thread = self.store.load(command.thread_id)
        retailer = self.retailer(thread.case_id)
        if d.mode == "standalone":
            from standalone import execute

            async with AsyncSqliteSaver.from_conn_string(str(self.store.path)) as saver:
                graph, middleware = build_standalone_agent(
                    self._model(), retailer, self.store, thread.id, saver
                )
                await execute(self.store, retailer, graph, middleware, command.id)
            return self.snapshot(thread.id)

        async with AsyncSqliteSaver.from_conn_string(str(self.store.path)) as saver:
            async with build_integrated_agent(
                self._model(), retailer, self.store, thread.id, saver, key
            ) as owner:
                await self._record_catalog(owner)
                request = json.loads(command.request_json)
                if command.kind == "start":
                    await owner.ainvoke_durable(
                        {"messages": [{"role": "user", "content": request["message"]}]},
                        thread_id=thread.id,
                        command_id=command.id,
                    )
                elif command.kind == "reply":
                    await owner.areply(
                        request["message"], thread_id=thread.id, command_id=command.id
                    )
                else:
                    await owner.aresume(thread_id=thread.id, command_id=command.id)
        return self.snapshot(thread.id)
