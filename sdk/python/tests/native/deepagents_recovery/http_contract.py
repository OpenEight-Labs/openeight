"""Real HTTP qualification driver for the exact durable registry tuple."""

import asyncio
import json
import os
import sys
import traceback
from contextlib import AsyncExitStack
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Annotated

from langchain.agents.middleware import TodoListMiddleware
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import InjectedToolArg, tool

from openeight import ConfigurationError, OpenEightConfig, SDKError, _framework
from openeight.deepagents import (
    DeepAgentsDeploymentConfig,
    DeepAgentsExecutionStore,
    DurableApprovalConfig,
    DurableExecutionStore,
    LocalDurability,
    ReconciliationResult,
    ToolSurface,
    _builder,
    create_deep_agent,
)
from openeight.deepagents._deployment import canonical, credential_binding
from openeight.langchain import _durable_execution


class ApplicationStore:
    """Application-owned protocol object backed by the shared SQLite implementation."""

    __slots__ = ("__backend", "settle_generated_attempt", "settle_command")
    __methods = frozenset(
        name
        for protocol in (DurableExecutionStore, DeepAgentsExecutionStore)
        for name, method in vars(protocol).items()
        if not name.startswith("_") and callable(method)
    )

    def __init__(self, backend):
        self.__backend = backend

    def __getattr__(self, name):
        if name not in self.__methods:
            raise AttributeError(name)
        return getattr(self.__backend, name)

    def admit(self, **request):
        return self.__backend.admit(**request)


class HistoryModel(FakeMessagesListChatModel):
    customer_workflow: bool = False

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, *args, **kwargs):
        if self.customer_workflow:
            latest = max(
                i for i, message in enumerate(messages) if message.type == "human"
            )
            if messages[latest].content == "go":
                from langchain_core.outputs import ChatGeneration, ChatResult

                return ChatResult(
                    generations=[
                        ChatGeneration(message=AIMessage(content="Please confirm"))
                    ]
                )
            messages = messages[latest:]
        self.i = min(sum(m.type == "ai" for m in messages), len(self.responses) - 1)
        return super()._generate(messages, *args, **kwargs)


def calls(name, args, call_id):
    return AIMessage(content="", tool_calls=[dict(name=name, args=args, id=call_id)])


def candidate_selection():
    selection = replace(
        _framework.select_compatibility(
            _framework.detect_framework("deepagents"), required=frozenset({"ordinary"})
        ),
        qualification_id="test-only-native-candidate",
        checkpoint_format="test-only-deep-sqlite-v1",
        supported_features=frozenset({"durable", "local_durable", "sync"}),
    )
    for module in (_framework, _builder, _durable_execution):
        module.select_compatibility = lambda *args, **kwargs: selection


async def run(request):
    async with AsyncExitStack() as stack:
        return await run_owned(request, stack)


async def run_owned(request, stack):
    if request.get("candidate"):
        candidate_selection()
    directory = Path(request["directory"])
    ledger = directory.parent / "effects.json"
    invocations = directory.parent / "handler-invocations"
    customer_pending = directory.parent / "customer-pending"

    async def classify(_):
        return (
            "waiting_customer"
            if request.get("customer") and customer_pending.exists()
            else "terminal"
        )

    @tool
    async def effect(
        value: str, idempotency_key: Annotated[str, InjectedToolArg]
    ) -> str:
        """Record the approved destination effect exactly once."""
        count = int(invocations.read_text()) if invocations.exists() else 0
        invocations.write_text(str(count + 1))
        invocations.chmod(0o600)
        if request.get("crash_before_effect"):
            os._exit(79)
        prior = json.loads(ledger.read_text()) if ledger.exists() else {}
        if idempotency_key in prior:
            assert prior[idempotency_key] == value
        else:
            prior[idempotency_key] = value
            ledger.write_text(json.dumps(prior, sort_keys=True))
            ledger.chmod(0o600)
        if request.get("crash_effect"):
            os._exit(79)
        if request.get("fail_effect"):
            raise RuntimeError("synthetic destination response loss")
        return value

    async def reconcile(attempt):
        if request.get("reconciliation_unknown"):
            return ReconciliationResult("unknown")
        prior = json.loads(ledger.read_text()) if ledger.exists() else {}
        if attempt.idempotency_key not in prior:
            return ReconciliationResult("not_executed")
        return ReconciliationResult(
            "confirmed",
            canonical(dict(content=prior[attempt.idempotency_key], status="success")),
        )

    if request.get("schema_variant"):
        effect.description = "Changed business tool contract."

    root = HistoryModel(
        customer_workflow=bool(request.get("customer")),
        responses=[
            calls(
                "write_todos",
                {"todos": [{"content": "investigate", "status": "in_progress"}]},
                "todos",
            ),
            calls(
                "write_file",
                {"file_path": "/root.txt", "content": "root artifact"},
                "root-file",
            ),
            calls(
                "task", {"description": "work", "subagent_type": "worker"}, "delegate"
            ),
            AIMessage(content="done"),
        ],
    )
    mutation = request.get("mutation")
    if mutation in {"edit_file", "delete"}:
        additions = []
        if mutation == "edit_file":
            additions.append(
                calls("read_file", {"file_path": "/root.txt"}, "read-root")
            )
            arguments = {
                "file_path": "/root.txt",
                "old_string": "root artifact",
                "new_string": "edited artifact",
            }
        else:
            arguments = {"file_path": "/root.txt"}
        additions.append(calls(mutation, arguments, "mutate-root"))
        root.responses[2:2] = additions
    child = dict(
        name="worker",
        description="work",
        system_prompt="work",
        tools=[effect],
        model=HistoryModel(
            responses=[
                calls(
                    "write_file",
                    {"file_path": "/child.txt", "content": "child artifact"},
                    "child-file",
                ),
                calls("effect", {"value": "once"}, "business"),
                AIMessage(content="child done"),
            ]
        ),
    )
    if request.get("nested"):
        from deepagents.backends import StateBackend
        from deepagents.middleware.subagents import SubAgentMiddleware

        leaf = dict(
            name="leaf",
            description="deepest work",
            system_prompt="leaf work",
            tools=[effect],
            middleware=[TodoListMiddleware()],
            model=HistoryModel(
                responses=[
                    calls(
                        "write_todos",
                        {"todos": [{"content": "leaf work", "status": "completed"}]},
                        "leaf-todos",
                    ),
                    calls("effect", {"value": "once"}, "same-call"),
                    AIMessage(content="leaf done"),
                ]
            ),
        )
        child["tools"] = []
        child["middleware"] = [
            SubAgentMiddleware(backend=StateBackend(), subagents=[leaf])
        ]
        child["model"].responses[1] = calls(
            "task", {"description": "leaf work", "subagent_type": "leaf"}, "same-call"
        )
        root.responses[2].tool_calls[0]["id"] = "same-call"
    governance = OpenEightConfig(
        agent_name=request["agent"],
        endpoint=request["endpoint"],
        credential=request["key"],
    )
    options = dict(
        durability=LocalDurability(
            directory=directory,
            revision=request.get("revision", "http-v1"),
            reconcile=reconcile,
            classify_turn=classify,
        )
    )
    runtime = None
    application_store = None
    if request.get("advanced"):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "langchain"))
        from _advanced_fixture import open_advanced_durability

        runtime = await stack.enter_async_context(
            open_advanced_durability(options["durability"])
        )
        with runtime.store.acquire_deployment():
            runtime.store.initialize_enrollment(
                canonical(
                    dict(
                        kind="verified_issuance",
                        endpoint=request["endpoint"],
                        agent_name=request["agent"],
                        **request["enrollment"],
                    )
                ),
                credential_binding(request["key"]),
            )

        application_store = ApplicationStore(runtime.store)
        governance = replace(
            governance,
            durable=DurableApprovalConfig(
                store=application_store, reconcile=reconcile, classify_turn=classify
            ),
        )
        options = dict(
            checkpointer=runtime.resources.checkpointer,
            durable_deployment=DeepAgentsDeploymentConfig(
                runtime.store.deployment().deployment_id,
                sha256(request.get("revision", "http-v1").encode()).hexdigest(),
                sha256(b"http-fixture-config-v1").hexdigest(),
            ),
        )
    agent = create_deep_agent(
        root,
        openeight=governance,
        **options,
        middleware=[TodoListMiddleware()],
        subagents=[child],
        include_general_purpose=False,
        tool_surfaces=[
            ToolSurface(
                (),
                (
                    "read_file",
                    "write_file",
                    "edit_file",
                    "delete",
                    "write_todos",
                    "task",
                ),
            ),
            ToolSurface(
                ("worker",),
                (
                    "read_file",
                    "write_file",
                    "task" if request.get("nested") else "effect",
                ),
            ),
            *(
                [ToolSurface(("worker", "leaf"), ("effect", "write_todos"))]
                if request.get("nested")
                else []
            ),
        ],
    )
    async with agent:
        store = application_store if runtime is not None else agent._local_runtime.store
        catalog = [
            dict(path=list(b.child_path), native=b.native_name, name=b.facts.name)
            for b in agent._owner.catalog.native.values()
        ]
        try:
            if runtime is None:
                await agent.ainitialize()
            else:
                await agent.abind_deployment()
        except ConfigurationError as error:
            if request["phase"] == "publish" and error.reason == "governance_not_ready":
                return dict(status="unconfigured", catalog=catalog)
            raise
        await agent.avalidate_deployment()
        await agent.areadiness()
        await agent.arefresh_tools()
        if request["phase"] == "initialize":
            return dict(status="ready", catalog=catalog)
        if request.get("crash_native"):
            settle = store.settle_generated_attempt

            def crash_after_receipt(thread_id, attempt_key, *args):
                result = settle(thread_id, attempt_key, *args)
                attempt = store.get_attempt(thread_id, attempt_key)
                call = json.loads(attempt.native_call_json)
                if call["name"] == request["crash_native"]:
                    os._exit(78)
                return result

            store.settle_generated_attempt = crash_after_receipt
        if request.get("crash_command"):
            settle_command = store.settle_command

            def crash_after_native_checkpoint(
                thread_id, command_id, revision, settlement
            ):
                if settlement.thread_status == "completed":
                    os._exit(80)
                return settle_command(thread_id, command_id, revision, settlement)

            store.settle_command = crash_after_native_checkpoint
        if request["phase"] == "start":
            if request.get("customer"):
                customer_pending.write_text("confirmation requested")
                customer_pending.chmod(0o600)
            value = {"messages": [{"role": "user", "content": "go"}]}
            if runtime is None:
                result = await agent.astart(value, command_id="start")
            else:
                with store.acquire_deployment():
                    admitted = store.admit(
                        kind="start", command_id="start", message="go"
                    )
                try:
                    result = await agent.ainvoke_durable(
                        value, thread_id=admitted.thread_id, command_id="start"
                    )
                except ConfigurationError as error:
                    if error.reason != "durable_recovery_required":
                        raise
                    return dict(
                        status="error",
                        reason=error.reason,
                        thread_id=admitted.thread_id,
                        run_id=admitted.run_id,
                        effects=len(json.loads(ledger.read_text()))
                        if ledger.exists()
                        else 0,
                        attempts=[
                            dict(decision_id=a.decision_id)
                            for a in store.attempts(admitted.thread_id)
                        ],
                    )
        elif request["phase"] == "reply":
            customer_pending.unlink(missing_ok=True)
            command_id = request.get("command_id", "reply")
            if runtime is not None:
                with store.acquire_deployment():
                    store.admit(
                        kind="reply",
                        thread_id=request["thread_id"],
                        command_id=command_id,
                        message="confirm",
                    )
                result = await agent.areply(
                    "confirm", thread_id=request["thread_id"], command_id=command_id
                )
            else:
                result = await agent.areply(
                    "confirm", thread_id=request["thread_id"], command_id=command_id
                )
        elif request["phase"] == "resume":
            if runtime is not None:
                with store.acquire_deployment():
                    store.admit(
                        kind="resume",
                        thread_id=request["thread_id"],
                        command_id=request.get("command_id", "resume"),
                    )
            result = await agent.aresume(
                thread_id=request["thread_id"],
                command_id=request.get("command_id", "resume"),
            )
        else:
            raise ValueError("unknown phase")
        attempts = store.attempts(result.thread_id)
        snapshot = await agent._owner._graph.aget_state(
            {"configurable": {"thread_id": result.thread_id}}
        )
        return dict(
            status=result.status,
            thread_id=result.thread_id,
            run_id=result.run_id,
            pending=list(result.pending_decision_ids),
            output=list(result.output),
            attempts=[
                dict(
                    decision_id=a.decision_id,
                    event_id=a.event_id,
                    status=a.status,
                    native=json.loads(a.native_call_json)["name"],
                    path=json.loads(
                        store.continuation(
                            result.thread_id, a.tool_call_id
                        ).child_path_json
                    ),
                )
                for a in attempts
            ],
            effects=len(json.loads(ledger.read_text())) if ledger.exists() else 0,
            handler_calls=int(invocations.read_text()) if invocations.exists() else 0,
            files=snapshot.values.get("files", {}),
            todos=snapshot.values.get("todos", []),
        )


if __name__ == "__main__":
    try:
        result = asyncio.run(run(json.load(sys.stdin)))
    except SDKError as error:
        result = dict(
            status="error",
            reason=getattr(error, "reason", None)
            or getattr(error, "code", None)
            or type(error).__name__,
            status_code=getattr(error, "status_code", None),
            frames=[
                dict(file=Path(f.filename).name, line=f.lineno, function=f.name)
                for f in traceback.extract_tb(error.__traceback__)
            ],
        )
    except Exception as error:
        # The fixture deliberately omits exception text, credentials, and provider bodies.
        result = dict(status="fixture_error", reason=type(error).__name__)
    print(json.dumps(result, sort_keys=True))
