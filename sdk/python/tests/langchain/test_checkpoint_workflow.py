"""Application-authorized enrollment and continuation use real native savers."""

import asyncio
import json
import os
from uuid import uuid4

import pytest

pytest.importorskip("langgraph.checkpoint.sqlite.aio")

import pytest_asyncio
from _checkpoint_application import ApplicationCheckpoints
from _external_control import ExternalControlPlane
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from test_framework_boundary import ToolModel

from openeight import ConfigurationError, _adapter_http
from openeight.langchain import create_agent

CONFIG = {
    "agent_name": "support",
    "endpoint": "https://control.example",
    "credential": "ctk_test",
}
THREAD = {"configurable": {"thread_id": "application-thread"}}
CONTEXT = {"subject": "scope-a"}


class Provider(ToolModel):
    calls: int = 0

    async def _agenerate(self, *args, **kwargs):
        self.calls += 1
        return await super()._agenerate(*args, **kwargs)


@pytest.mark.asyncio
async def test_interrupt_inside_effect_is_recovery_required_and_cannot_replay(
    tmp_path, service, real_saver
):
    from langchain_core.tools import tool
    from langgraph.types import Command, interrupt

    access = ApplicationCheckpoints(tmp_path / "claims.sqlite")
    effects = []

    @tool
    async def write_todos(value: str) -> str:
        """Perform an external effect before an unsupported application pause."""
        effects.append(value)
        interrupt(
            {
                "action_requests": [{"name": "write_todos", "args": {"value": value}}],
                "review_configs": [
                    {"action_name": "write_todos", "allowed_decisions": ["approve"]}
                ],
            }
        )
        return value

    model = Provider(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "effect", "name": "write_todos", "args": {"value": "once"}}
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    async with create_agent(
        model,
        [write_todos],
        openeight={**CONFIG, "checkpoints": access},
        checkpointer=real_saver,
    ) as agent:
        with pytest.raises(ConfigurationError):
            await agent.ainvoke({"messages": [("user", "go")]}, THREAD, context=CONTEXT)
        access.mode = "continue"
        with pytest.raises(ConfigurationError):
            await agent.ainvoke(Command(resume="approved"), THREAD, context=CONTEXT)
    assert effects == ["once"]
    with access.database() as database:
        records = [
            json.loads(row[0])
            for row in database.execute("SELECT body FROM executions")
        ]
        assert len(records) == 1 and records[0]["state"] == "recovery_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("native_pause", [True, False])
async def test_child_pause_requires_verified_native_hitl_lineage(
    tmp_path, service, real_saver, native_pause
):
    pytest.importorskip("deepagents")
    from deepagents.middleware.filesystem import FilesystemPermission
    from langchain_core.tools import tool
    from langgraph.types import Command, interrupt

    from openeight.deepagents import create_deep_agent

    access = ApplicationCheckpoints(tmp_path / "claims.sqlite")
    effects = []

    @tool
    async def write_todos(value: str) -> str:
        """Perform an application effect before an unsupported pause."""
        effects.append(value)
        interrupt("not a native approval boundary")
        return value

    def make(resuming=False):
        root = Provider(
            responses=[AIMessage(content="done")]
            if resuming
            else [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "delegate",
                            "name": "task",
                            "args": {"description": "work", "subagent_type": "worker"},
                        }
                    ],
                ),
                AIMessage(content="done"),
            ]
        )
        name, args = (
            ("write_file", {"file_path": "/child.txt", "content": "child"})
            if native_pause
            else ("write_todos", {"value": "once"})
        )
        child = {
            "name": "worker",
            "description": "work",
            "model": Provider(
                responses=[AIMessage(content="done")]
                if resuming
                else [
                    AIMessage(
                        content="",
                        tool_calls=[{"id": "child-effect", "name": name, "args": args}],
                    ),
                    AIMessage(content="done"),
                ]
            ),
            "tools": [] if native_pause else [write_todos],
        }
        if native_pause:
            child["permissions"] = [
                FilesystemPermission(
                    operations=["write"], paths=["/**"], mode="interrupt"
                )
            ]
        return create_deep_agent(
            root,
            subagents=[child],
            include_general_purpose=False,
            catalog_naming="qualified-v1",
            openeight={**CONFIG, "checkpoints": access},
            checkpointer=real_saver,
        )

    async with make() as agent:
        if native_pause:
            result = await agent.ainvoke(
                {"messages": [("user", "work")]}, THREAD, context=CONTEXT
            )
            assert result["__interrupt__"]
        else:
            with pytest.raises(ConfigurationError):
                await agent.ainvoke(
                    {"messages": [("user", "work")]}, THREAD, context=CONTEXT
                )
    access.mode = "continue"
    async with make(resuming=True) as agent:
        if native_pause:
            result = await agent.ainvoke(
                Command(resume={"decisions": [{"type": "approve"}]}),
                THREAD,
                context=CONTEXT,
            )
            assert (
                not result.get("__interrupt__")
                and result["messages"][-1].content == "done"
            )
        else:
            with pytest.raises(ConfigurationError):
                await agent.ainvoke(Command(resume="approved"), THREAD, context=CONTEXT)
    assert effects == ([] if native_pause else ["once"])
    with access.database() as database:
        states = [
            json.loads(row[0])["state"]
            for row in database.execute("SELECT body FROM executions ORDER BY rowid")
        ]
        assert states == (
            ["native_interrupt", "settled"] if native_pause else ["recovery_required"]
        )


@pytest.mark.asyncio
async def test_partial_sibling_native_resume_preserves_completed_child_effect(
    tmp_path, service, real_saver
):
    pytest.importorskip("deepagents")
    from deepagents.middleware.filesystem import FilesystemPermission
    from langchain_core.messages import ToolMessage
    from langgraph.types import Command

    from openeight.deepagents import create_deep_agent
    from openeight.deepagents._frameworks._inspection import native_graphs

    access = ApplicationCheckpoints(tmp_path / "claims.sqlite")
    calls = [
        {
            "id": name,
            "name": "task",
            "args": {"description": "work", "subagent_type": name},
        }
        for name in ("a", "b")
    ]
    children = [
        {
            "name": name,
            "description": "work",
            "permissions": [
                FilesystemPermission(
                    operations=["write"], paths=["/**"], mode="interrupt"
                )
            ],
            "model": Provider(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": name + "-write",
                                "name": "write_file",
                                "args": {
                                    "file_path": "/" + name + ".txt",
                                    "content": name,
                                },
                            }
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            ),
        }
        for name in ("a", "b")
    ]
    agent = create_deep_agent(
        Provider(
            responses=[
                AIMessage(content="", tool_calls=calls),
                AIMessage(content="done"),
            ]
        ),
        subagents=children,
        include_general_purpose=False,
        catalog_naming="qualified-v1",
        openeight={**CONFIG, "checkpoints": access},
        checkpointer=real_saver,
    )
    async with agent:
        paused = await agent.ainvoke(
            {"messages": [("user", "work")]}, THREAD, context=CONTEXT
        )
        identities = {
            item.value["action_requests"][0]["args"]["file_path"]: item.id
            for item in paused["__interrupt__"]
        }
        assert set(identities) == {"/a.txt", "/b.txt"}
        built = agent._session._generation_builds["__static__"]
        graphs = native_graphs(
            built.graph, {control.child_path: control for control in built.controls}
        )
        root_saved = await real_saver.aget_tuple(THREAD)
        root_state = await graphs[()].aget_state(
            {
                "configurable": {
                    **THREAD["configurable"],
                    "__pregel_checkpointer": real_saver,
                }
            }
        )
        tasks = root_saved.checkpoint["channel_values"]["__pregel_tasks"]
        a_task = next(
            task
            for task in root_state.tasks
            if tasks[task.path[1]].arg[0]["args"]["subagent_type"] == "a"
        )
        a_config = {
            "configurable": {
                "thread_id": "application-thread",
                "checkpoint_ns": "tools:" + a_task.id,
                "__pregel_checkpointer": real_saver,
            }
        }
        access.mode = "continue"
        partial = await agent.ainvoke(
            Command(
                resume={identities["/a.txt"]: {"decisions": [{"type": "approve"}]}}
            ),
            THREAD,
            context=CONTEXT,
        )
        assert {item.id for item in partial["__interrupt__"]} == {identities["/b.txt"]}
        a_saved = await real_saver.aget_tuple(a_config)
        a_state = await graphs[("a",)].aget_state(a_config)
        a_results = [
            message
            for message in a_state.values["messages"]
            if isinstance(message, ToolMessage) and message.name == "write_file"
        ]
        assert len(a_results) == 1 and a_results[0].status == "success"
        done = await agent.ainvoke(
            Command(
                resume={identities["/b.txt"]: {"decisions": [{"type": "approve"}]}}
            ),
            THREAD,
            context=CONTEXT,
        )
        assert not done.get("__interrupt__")
        assert (await real_saver.aget_tuple(a_config)).config == a_saved.config
    with access.database() as database:
        records = [
            json.loads(row[0])
            for row in database.execute("SELECT body FROM executions ORDER BY rowid")
        ]
        assert [record["state"] for record in records] == [
            "native_interrupt",
            "native_interrupt",
            "settled",
        ]
        assert records[0]["output_checkpoint_id"] == records[1]["output_checkpoint_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_repeated_cancellation_preserves_same_context_claim_exit(
    tmp_path, service, real_saver, streaming
):
    from contextlib import asynccontextmanager
    from contextvars import ContextVar

    authenticated = ContextVar("application_authenticated_claim", default=None)
    exiting, release = asyncio.Event(), asyncio.Event()
    completed = []

    class ContextualAccess(ApplicationCheckpoints):
        @asynccontextmanager
        async def claim(self, *, config, context):
            token = authenticated.set(context["subject"])
            try:
                async with super().claim(config=config, context=context) as claim:
                    yield claim
                    exiting.set()
                    await release.wait()
            finally:
                authenticated.reset(token)
                completed.append(True)

    access = ContextualAccess(tmp_path / "claims.sqlite")
    async with create_agent(
        Provider(responses=[AIMessage(content="done")]),
        openeight={**CONFIG, "checkpoints": access},
        checkpointer=real_saver,
    ) as agent:

        async def execute():
            if streaming:
                return [
                    item
                    async for item in agent.astream(
                        {"messages": [("user", "go")]}, THREAD, context=CONTEXT
                    )
                ]
            return await agent.ainvoke(
                {"messages": [("user", "go")]}, THREAD, context=CONTEXT
            )

        task = asyncio.create_task(execute())
        await asyncio.wait_for(exiting.wait(), 3)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done() and access.guard.locked() and completed == []
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert completed == [True] and not access.guard.locked()
        assert authenticated.get() is None


@pytest.fixture
def service(monkeypatch):
    result = ExternalControlPlane()
    monkeypatch.setattr(_adapter_http, "DeadlineHTTPTransport", lambda: result)
    return result


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def real_saver(request, tmp_path):
    if request.param == "sqlite":
        async with AsyncSqliteSaver.from_conn_string(
            str(tmp_path / "native.sqlite")
        ) as saver:
            yield saver
        return
    connection_string = os.environ.get("OPENEIGHT_TEST_POSTGRES_DSN")
    if not connection_string:
        pytest.skip("Requires the isolated PostgreSQL qualification database")
    psycopg = pytest.importorskip("psycopg")
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg import sql
    from psycopg.rows import dict_row

    schema = "openeight_checkpoint_qualification_" + uuid4().hex
    async with await psycopg.AsyncConnection.connect(
        connection_string, autocommit=True, prepare_threshold=0, row_factory=dict_row
    ) as connection:
        await connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
        try:
            await connection.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
            )
            saver = AsyncPostgresSaver(connection)
            await saver.setup()
            yield saver
        finally:
            await connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("constructor", ["langchain", "deepagents"], indirect=True)
async def test_real_saver_continuation_and_stale_checkpoint_fencing(
    tmp_path, service, real_saver, constructor
):
    access = ApplicationCheckpoints(tmp_path / "claims.sqlite")
    prior = None
    for turn in ("first", "second"):
        async with constructor(
            Provider(responses=[AIMessage(content=turn)]),
            openeight={**CONFIG, "checkpoints": access},
            checkpointer=real_saver,
        ) as agent:
            result = await agent.ainvoke(
                {"messages": [("user", turn)]}, THREAD, context=CONTEXT
            )
            assert result["messages"][-1].content == turn
            if turn == "second":
                assert len(result["messages"]) == 4
                with pytest.raises(ConfigurationError):
                    await agent.ainvoke(
                        {"messages": [("user", "stale")]}, prior.config, context=CONTEXT
                    )
            prior = await real_saver.aget_tuple(THREAD)
        access.mode = "continue"
    with access.database() as database:
        rows = [
            json.loads(row[0])
            for row in database.execute("SELECT body FROM executions")
        ]
        assert len(rows) == 2 and all(record["state"] == "settled" for record in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("constructor", ["langchain", "deepagents"], indirect=True)
async def test_new_enrollment_and_restart_preserve_history(
    tmp_path, service, constructor
):
    sidecar, checkpoints = tmp_path / "claims.sqlite", tmp_path / "native.sqlite"
    for turn in ("first", "second"):
        access = ApplicationCheckpoints(sidecar)
        access.mode = "new" if turn == "first" else "continue"
        async with AsyncSqliteSaver.from_conn_string(str(checkpoints)) as saver:
            async with constructor(
                ToolModel(responses=[AIMessage(content=turn)]),
                openeight={**CONFIG, "checkpoints": access},
                checkpointer=saver,
            ) as agent:
                result = await agent.ainvoke(
                    {"messages": [("user", turn)]}, THREAD, context=CONTEXT
                )
                assert result["messages"][-1].content == turn
                if turn == "second":
                    assert [message.content for message in result["messages"]] == [
                        "first",
                        "first",
                        "second",
                        "second",
                    ]
                native = await saver.aget_tuple(THREAD)
                assert (
                    native.metadata["openeight_checkpoint"]["graph_revision"]
                    == "release-1"
                )
        with access.database() as database:
            records = [
                json.loads(row[0])
                for row in database.execute("SELECT body FROM executions")
            ]
            assert all(record["state"] == "settled" for record in records)
            assert len(records) == (1 if turn == "first" else 2)


@pytest.mark.asyncio
async def test_unauthorized_claim_has_no_native_history_or_provider_calls(
    tmp_path, service, real_saver
):
    access = ApplicationCheckpoints(tmp_path / "claims.sqlite")
    model = Provider(responses=[AIMessage(content="forbidden")])
    saver = real_saver
    async with create_agent(
        model, openeight={**CONFIG, "checkpoints": access}, checkpointer=saver
    ) as agent:
        with pytest.raises(ConfigurationError) as caught:
            await agent.ainvoke(
                {"messages": [("user", "hello")]},
                THREAD,
                context={"subject": "foreign"},
            )
        assert caught.value.reason == "checkpoint_scope_invalid"
        assert caught.value.__context__ is None
        assert model.calls == 0
        assert await saver.aget_tuple(THREAD) is None
    with access.database() as database:
        assert database.execute("SELECT count(*) FROM enrollment").fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["credential", "endpoint", "agent_name", "revision", "scope"]
)
async def test_changed_enrollment_rejects_without_mutation_or_provider_call(
    tmp_path, service, change, real_saver
):
    access = ApplicationCheckpoints(tmp_path / "claims.sqlite")
    saver = real_saver
    async with create_agent(
        Provider(responses=[AIMessage(content="first")]),
        openeight={**CONFIG, "checkpoints": access},
        checkpointer=saver,
    ) as agent:
        await agent.ainvoke({"messages": [("user", "first")]}, THREAD, context=CONTEXT)
    before = await saver.aget_tuple(THREAD)
    access.mode = "continue"
    config = dict(CONFIG)
    context = dict(CONTEXT)
    if change == "revision":
        access.graph_revision = "release-2"
    elif change == "scope":
        access.scope_id = context["subject"] = "scope-b"
    else:
        config[change] = {
            "credential": "ctk_rotated",
            "endpoint": "https://other.example",
            "agent_name": "other",
        }[change]
    model = Provider(responses=[AIMessage(content="forbidden")])
    async with create_agent(
        model, openeight={**config, "checkpoints": access}, checkpointer=saver
    ) as agent:
        with pytest.raises(ConfigurationError):
            await agent.ainvoke(
                {"messages": [("user", "next")]}, THREAD, context=context
            )
    assert model.calls == 0
    assert (await saver.aget_tuple(THREAD)) == before
    with access.database() as database:
        assert database.execute("SELECT count(*) FROM executions").fetchone() == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize("constructor", ["langchain", "deepagents"], indirect=True)
@pytest.mark.parametrize("stale", [False, True])
async def test_native_completed_history_adoption_requires_exact_current_checkpoint(
    tmp_path, service, constructor, stale, real_saver
):
    from langchain.agents import create_agent as native_langchain

    if constructor is create_agent:
        native_constructor = native_langchain
    else:
        from deepagents import create_deep_agent as native_constructor
    access = ApplicationCheckpoints(tmp_path / "claims.sqlite")
    access.mode = "adopt"
    saver = real_saver
    native = native_constructor(
        Provider(responses=[AIMessage(content="historical")]), checkpointer=saver
    )
    await native.ainvoke({"messages": [("user", "old request")]}, THREAD)
    checkpoint = await saver.aget_tuple(THREAD)
    selected = checkpoint.parent_config if stale else checkpoint.config
    model = Provider(responses=[AIMessage(content="new governed turn")])
    async with constructor(
        model, openeight={**CONFIG, "checkpoints": access}, checkpointer=saver
    ) as agent:
        if stale:
            with pytest.raises(ConfigurationError):
                await agent.ainvoke(
                    {"messages": [("user", "new request")]},
                    selected,
                    context=CONTEXT,
                )
            assert model.calls == 0 and await saver.aget_tuple(THREAD) == checkpoint
        else:
            result = await agent.ainvoke(
                {"messages": [("user", "new request")]}, selected, context=CONTEXT
            )
            assert [message.content for message in result["messages"]] == [
                "old request",
                "historical",
                "new request",
                "new governed turn",
            ]
    with access.database() as database:
        row = database.execute("SELECT body FROM enrollment").fetchone()
        if stale:
            assert row is None
        else:
            enrollment = json.loads(row[0])
            assert enrollment["origin"] == "adopted"
            assert (
                enrollment["adoption_checkpoint_id"]
                == checkpoint.config["configurable"]["checkpoint_id"]
            )


@pytest.mark.asyncio
async def test_claim_loss_during_approval_blocks_exact_replayed_decision_dispatch(
    tmp_path, service, monkeypatch, real_saver
):
    from langchain_core.tools import tool

    from openeight.langchain import ApprovalConfig

    access = ApplicationCheckpoints(tmp_path / "claims.sqlite")
    effects = []

    @tool
    async def write_file(value: str) -> str:
        """Write one external value."""
        effects.append(value)
        return value

    service.verdict, service.replay_verdict, service.approval_state = (
        "approval_required",
        "allow",
        "approved",
    )
    send = service.send

    def external(request, deadline):
        if request.url.path.endswith("/GetApprovalState"):
            access.authorized = False
        return send(request, deadline)

    monkeypatch.setattr(service, "send", external)
    model = Provider(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "effect",
                        "name": "write_file",
                        "args": {"value": "must not execute"},
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    saver = real_saver
    async with create_agent(
        model,
        [write_file],
        openeight={
            **CONFIG,
            "checkpoints": access,
            "approval": ApprovalConfig(
                timeout=1, poll_interval=0.001, poll_max_interval=0.002
            ),
        },
        checkpointer=saver,
    ) as agent:
        with pytest.raises(ConfigurationError):
            await agent.ainvoke({"messages": [("user", "go")]}, THREAD, context=CONTEXT)
    decisions = [body for operation, body in service.requests if operation == "Decide"]
    assert len(decisions) == 2 and decisions[0] == decisions[1]
    assert effects == [] and not access.guard.locked()
    with access.database() as database:
        assert (
            json.loads(database.execute("SELECT body FROM executions").fetchone()[0])[
                "state"
            ]
            == "running"
        )


@pytest.mark.asyncio
async def test_native_hitl_restart_pins_variant_and_rechecks_final_arguments(
    tmp_path, service, real_saver
):
    pytest.importorskip("deepagents")
    from deepagents.middleware.filesystem import FilesystemPermission
    from langchain_core.runnables import ConfigurableField
    from langgraph.types import Command

    from openeight import ModelVariant, RuntimeModelConfig
    from openeight.deepagents import create_deep_agent

    access = ApplicationCheckpoints(tmp_path / "claims.sqlite")
    runtime = RuntimeModelConfig(
        variants=(
            ModelVariant(name="first", configurable={"choice": "first"}),
            ModelVariant(name="second", configurable={"choice": "second"}),
        )
    )
    permissions = [
        FilesystemPermission(operations=["write"], paths=["/**"], mode="interrupt")
    ]
    config = {"configurable": {"thread_id": "application-thread", "choice": "first"}}

    def make(saver, responses):
        first = Provider(responses=responses)
        model = first.configurable_alternatives(
            ConfigurableField(id="choice"),
            default_key="first",
            second=Provider(responses=[AIMessage(content="second")]),
        )
        return create_deep_agent(
            model,
            openeight={**CONFIG, "checkpoints": access, "runtime_models": runtime},
            checkpointer=saver,
            permissions=permissions,
            include_general_purpose=False,
            catalog_naming="qualified-v1",
        )

    saver = real_saver
    async with make(
        saver,
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "write",
                        "name": "write_file",
                        "args": {
                            "file_path": "/original.txt",
                            "content": "content",
                        },
                    }
                ],
            ),
            AIMessage(content="done"),
        ],
    ) as agent:
        paused = await agent.ainvoke(
            {"messages": [("user", "write")]},
            config,
            context={**CONTEXT, "variant": "first"},
        )
        assert paused["__interrupt__"] and not paused.get("files")
    access.mode = "continue"
    async with make(saver, [AIMessage(content="done")]) as agent:
        command = Command(
            resume={
                "decisions": [
                    {
                        "type": "edit",
                        "edited_action": {
                            "name": "write_file",
                            "args": {
                                "file_path": "/edited.txt",
                                "content": "edited",
                            },
                        },
                    }
                ]
            }
        )
        with pytest.raises(ConfigurationError):
            await agent.ainvoke(
                command,
                {
                    "configurable": {
                        "thread_id": "application-thread",
                        "choice": "second",
                    }
                },
                context={**CONTEXT, "variant": "second"},
            )
        result = await agent.ainvoke(
            command, config, context={**CONTEXT, "variant": "first"}
        )
        assert (
            "/edited.txt" in result["files"] and "/original.txt" not in result["files"]
        )
    with access.database() as database:
        records = [
            json.loads(row[0])
            for row in database.execute("SELECT body FROM executions ORDER BY rowid")
        ]
        assert [record["state"] for record in records] == [
            "native_interrupt",
            "settled",
        ]
        assert {record["model_variant"] for record in records} == {"first"}


@pytest.mark.asyncio
async def test_cancelled_effect_retains_claim_and_blocks_uncertain_replay(
    tmp_path, service, real_saver
):
    from langchain_core.tools import tool

    from openeight import CleanupIncompleteError

    access = ApplicationCheckpoints(tmp_path / "claims.sqlite")
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    @tool
    async def write_file(value: str) -> str:
        """Hold an admitted external effect."""
        entered.set()
        await release.wait()
        effects.append(value)
        return value

    saver = real_saver
    agent = create_agent(
        Provider(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "effect",
                            "name": "write_file",
                            "args": {"value": "once"},
                        }
                    ],
                ),
                AIMessage(content="done"),
            ]
        ),
        [write_file],
        openeight={**CONFIG, "checkpoints": access},
        checkpointer=saver,
    )
    async with agent:
        execution = asyncio.create_task(
            agent.ainvoke({"messages": [("user", "go")]}, THREAD, context=CONTEXT)
        )
        await asyncio.wait_for(entered.wait(), 3)
        execution.cancel()
        await asyncio.sleep(0)
        with pytest.raises(CleanupIncompleteError):
            await agent.aclose(0.001)
        assert access.guard.locked() and not execution.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await execution
    assert effects == ["once"] and not access.guard.locked()
    access.mode = "continue"
    model = Provider(responses=[AIMessage(content="forbidden")])
    async with create_agent(
        model,
        [write_file],
        openeight={**CONFIG, "checkpoints": access},
        checkpointer=saver,
    ) as reopened:
        with pytest.raises(ConfigurationError):
            await reopened.ainvoke(
                {"messages": [("user", "retry")]}, THREAD, context=CONTEXT
            )
        assert model.calls == 0 and effects == ["once"]
    with access.database() as database:
        assert (
            json.loads(database.execute("SELECT body FROM executions").fetchone()[0])[
                "state"
            ]
            == "recovery_required"
        )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("OPENEIGHT_COOKBOOK_ROOT"),
    reason="Set OPENEIGHT_COOKBOOK_ROOT for cookbook qualification",
)
async def test_public_application_adapter_continues_real_saver_across_sessions(
    tmp_path, service, real_saver
):
    import importlib.util

    from _cookbook import recipe

    from openeight.checkpoints import CheckpointBinding

    source = recipe("patterns/checkpoint-recovery") / "application.py"
    spec = importlib.util.spec_from_file_location("checkpoint_example", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    authenticated = object()
    mode = "new"

    async def authorize(*, config, context):
        if context is not authenticated or config != THREAD:
            raise PermissionError("not authenticated for this thread")
        return CheckpointBinding(
            scope_id="scope-a",
            thread_id="application-thread",
            checkpoint_ns="",
            graph_revision="release-1",
            model_variant=None,
            mode=mode,
        )

    access = module.ApplicationCheckpoints(
        tmp_path / "public-sidecar.sqlite", authorize=authorize
    )
    for turn in range(2):
        async with create_agent(
            Provider(responses=[AIMessage(content="done")]),
            openeight={**CONFIG, "checkpoints": access},
            checkpointer=real_saver,
        ) as agent:
            result = await agent.ainvoke(
                {"messages": [("user", f"turn-{turn}")]}, THREAD, context=authenticated
            )
            assert result["messages"][-1].content == "done"
        mode = "continue"
    async with access.claim(config=THREAD, context=authenticated) as claim:
        assert (await claim.last_execution()).state == "settled"
        assert (await claim.read_binding()).graph_revision == "release-1"
    with pytest.raises(PermissionError):
        await claim.assert_held()
    async with access.claim(config=THREAD, context=authenticated):
        with pytest.raises(PermissionError):
            await claim.assert_held()

    from dataclasses import replace

    async with access.claim(config=THREAD, context=authenticated) as claim:
        prior = await claim.last_execution()
        running = replace(
            prior,
            execution_id=str(uuid4()),
            state="running",
            finished_at=None,
            output_checkpoint_id=None,
        )
        await claim.begin(running)
        settled = replace(
            running,
            state="settled",
            finished_at=prior.finished_at,
            output_checkpoint_id=prior.output_checkpoint_id,
        )
        for invalid in (
            replace(settled, run_id=str(uuid4())),
            replace(settled, started_at="2026-01-01T00:00:00Z"),
            replace(settled, input_checkpoint_id="foreign"),
            replace(settled, model_variant="foreign"),
            replace(settled, state="running"),
            replace(settled, finished_at=None),
            replace(settled, output_checkpoint_id=None),
        ):
            with pytest.raises(ValueError):
                await claim.finish(invalid)
            assert await claim.last_execution() == running
        await claim.finish(settled)
        await claim.finish(settled)
    empty = module.ApplicationCheckpoints(
        tmp_path / "empty-sidecar.sqlite", authorize=authorize
    )
    async with empty.claim(config=THREAD, context=authenticated) as claim:
        with pytest.raises(ValueError, match="enrollment"):
            await claim.begin(running)


@pytest.mark.skipif(
    not os.environ.get("OPENEIGHT_COOKBOOK_ROOT"),
    reason="Set OPENEIGHT_COOKBOOK_ROOT for cookbook qualification",
)
def test_public_checkpoint_workflow_survives_real_process_restart(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    probe = Path(__file__).with_name("_ordinary_checkpoint_probe.py")
    for mode in ("new", "continue"):
        result = subprocess.run(
            [sys.executable, str(probe), str(tmp_path), mode],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "qualified " + mode


@pytest.fixture
def constructor(request):
    if request.param == "langchain":
        return create_agent
    pytest.importorskip("deepagents")
    from openeight.deepagents import create_deep_agent

    return create_deep_agent
