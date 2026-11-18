"""Native permission ordering and OpenEight enforcement through real tool dispatch."""

import asyncio
import json
import sys

import pytest

pytest.importorskip("deepagents")

from _external_control import ExternalControlPlane
from deepagents.middleware.filesystem import FilesystemPermission
from langchain_core.messages import AIMessage
from test_framework_boundary import ToolModel

from openeight import ConfigurationError, _adapter_http
from openeight.deepagents import create_deep_agent
from openeight.langchain import ApprovalConfig

CONFIG = {
    "agent_name": "support",
    "endpoint": "https://control.example",
    "credential": "ctk_test",
}


@pytest.fixture
def control(monkeypatch):
    service = ExternalControlPlane()
    monkeypatch.setattr(_adapter_http, "DeadlineHTTPTransport", lambda: service)
    return service


def model():
    return ToolModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "id": "write",
                        "args": {
                            "file_path": "/secrets/test.txt",
                            "content": "classified",
                        },
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )


@pytest.mark.parametrize(
    "modes,allowed",
    [(["deny"], False), (["allow", "deny"], True), (["deny", "allow"], False)],
)
def test_native_first_match_rules_control_actual_file_state(control, modes, allowed):
    rules = [
        FilesystemPermission(operations=["write"], paths=["/**"], mode=mode)
        for mode in modes
    ]

    async def run():
        agent = create_deep_agent(
            model(),
            openeight=CONFIG,
            permissions=rules,
            include_general_purpose=False,
            catalog_naming="qualified-v1",
        )
        result = await agent.ainvoke({"messages": [("user", "write")]})
        assert ("/secrets/test.txt" in result.get("files", {})) is allowed
        assert len(control.decisions) == 1
        if not allowed:
            assert any(
                "permission denied" in str(message.content)
                for message in result["messages"]
            )

    asyncio.run(run())


@pytest.mark.parametrize("verdict", ["deny", "approval_required"])
def test_openeight_block_never_enters_native_filesystem_handler(control, verdict):
    control.verdict = verdict
    entered = []

    def profile(frame, event, arg):
        if (
            event == "call"
            and "FilesystemMiddleware._create_write_file_tool.<locals>.async_write_file"
            in frame.f_code.co_qualname
        ):
            entered.append(True)

    async def run():
        agent = create_deep_agent(
            model(),
            openeight={
                **CONFIG,
                "approval": ApprovalConfig(
                    timeout=0.01, poll_interval=0.001, poll_max_interval=0.002
                ),
            },
            permissions=[
                FilesystemPermission(operations=["write"], paths=["/**"], mode="allow")
            ],
            include_general_purpose=False,
            catalog_naming="qualified-v1",
        )
        previous = sys.getprofile()
        sys.setprofile(profile)
        try:
            result = await agent.ainvoke({"messages": [("user", "write")]})
        finally:
            sys.setprofile(previous)
        assert not result.get("files")
        assert not entered
        assert len(control.decisions) == 1

    asyncio.run(run())


def test_permission_interrupt_requires_ordinary_persistent_saver():
    with pytest.raises(ConfigurationError) as caught:
        create_deep_agent(
            model(),
            openeight=CONFIG,
            permissions=[
                FilesystemPermission(
                    operations=["write"], paths=["/**"], mode="interrupt"
                )
            ],
        )
    assert caught.value.reason == "unsupported_checkpointer"


def test_explicit_false_interrupt_override_preserves_native_no_saver_execution(control):
    async def run():
        agent = create_deep_agent(
            model(),
            openeight=CONFIG,
            include_general_purpose=False,
            permissions=[
                FilesystemPermission(
                    operations=["write"], paths=["/**"], mode="interrupt"
                )
            ],
            interrupt_on={
                "ls": False,
                "read_file": False,
                "write_file": False,
                "edit_file": False,
                "delete": False,
                "glob": False,
                "grep": False,
            },
            catalog_naming="qualified-v1",
        )
        result = await agent.ainvoke({"messages": [("user", "write")]})
        assert "/secrets/test.txt" in result["files"]
        assert len(control.decisions) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "destination,allowed",
    [("/secrets/edited.txt", True), ("/denied/edited.txt", False)],
)
def test_native_hitl_edits_pass_final_permissions_and_openeight(
    control, destination, allowed
):
    pytest.importorskip("langgraph.checkpoint.sqlite.aio")

    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.types import Command

    control.field_selectors = ["file_path"]

    async def run():
        async with AsyncSqliteSaver.from_conn_string(":memory:") as saver:
            saver.serde = JsonPlusSerializer(pickle_fallback=False)
            agent = create_deep_agent(
                model(),
                openeight=CONFIG,
                checkpointer=saver,
                permissions=[
                    FilesystemPermission(
                        operations=["write"], paths=["/denied/**"], mode="deny"
                    ),
                    FilesystemPermission(
                        operations=["write"], paths=["/secrets/**"], mode="interrupt"
                    ),
                ],
                include_general_purpose=False,
                catalog_naming="qualified-v1",
            )
            async with agent:
                thread = agent.new_thread()
                config = {"configurable": {"thread_id": thread}}
                paused = await agent.ainvoke({"messages": [("user", "write")]}, config)
                assert paused.get("__interrupt__")
                assert not paused.get("files") and not control.decisions
                result = await agent.ainvoke(
                    Command(
                        resume={
                            "decisions": [
                                {
                                    "type": "edit",
                                    "edited_action": {
                                        "name": "write_file",
                                        "args": {
                                            "file_path": destination,
                                            "content": "edited",
                                        },
                                    },
                                }
                            ]
                        }
                    ),
                    config,
                )
                assert (destination in result.get("files", {})) is allowed
                assert "/secrets/test.txt" not in result.get("files", {})
                assert len(control.decisions) == 1
                decision = next(iter(control.decisions.values()))
                assert destination in json.dumps(decision)
                assert "/secrets/test.txt" not in json.dumps(decision)

    asyncio.run(run())


@pytest.mark.parametrize("replacement,allowed", [(False, False), (True, True)])
def test_child_permissions_inherit_or_replace_during_actual_delegation(
    control, replacement, allowed
):
    child = {"name": "worker", "description": "Write the requested file"}
    if replacement:
        child["permissions"] = []
    provider = ToolModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "id": "delegate",
                        "args": {
                            "description": "Write the file",
                            "subagent_type": "worker",
                        },
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "id": "write",
                        "args": {"file_path": "/secrets/test.txt", "content": "child"},
                    }
                ],
            ),
            AIMessage(content="child done"),
            AIMessage(content="parent done"),
        ]
    )

    async def run():
        agent = create_deep_agent(
            provider,
            openeight=CONFIG,
            subagents=[child],
            include_general_purpose=False,
            permissions=[
                FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")
            ],
            catalog_naming="qualified-v1",
        )
        result = await agent.ainvoke({"messages": [("user", "delegate")]})
        assert ("/secrets/test.txt" in result.get("files", {})) is allowed
        assert len(control.decisions) == 2

    asyncio.run(run())


def test_tool_middleware_cannot_introduce_an_unreviewed_interrupt_path(control):
    pytest.importorskip("langgraph.checkpoint.sqlite.aio")

    from langchain.agents.middleware import AgentMiddleware
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    class EditPath(AgentMiddleware):
        async def awrap_tool_call(self, request, handler):
            return await handler(
                request.override(
                    tool_call={
                        **request.tool_call,
                        "args": {
                            "file_path": "/secrets/edited.txt",
                            "content": "edited",
                        },
                    }
                )
            )

    provider = ToolModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "id": "write",
                        "args": {"file_path": "/public/test.txt", "content": "public"},
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )

    async def run():
        async with AsyncSqliteSaver.from_conn_string(":memory:") as saver:
            saver.serde = JsonPlusSerializer(pickle_fallback=False)
            agent = create_deep_agent(
                provider,
                openeight=CONFIG,
                middleware=[EditPath()],
                checkpointer=saver,
                permissions=[
                    FilesystemPermission(
                        operations=["write"], paths=["/secrets/**"], mode="interrupt"
                    )
                ],
                include_general_purpose=False,
                catalog_naming="qualified-v1",
            )
            async with agent:
                config = {"configurable": {"thread_id": agent.new_thread()}}
                with pytest.raises(ConfigurationError) as caught:
                    await agent.ainvoke({"messages": [("user", "write")]}, config)
                assert caught.value.reason == "checkpoint_resume_invalid"
                snapshot = await agent._session._saver.aget_tuple(config)
                assert not snapshot.checkpoint["channel_values"].get("files")
                assert not control.decisions

    asyncio.run(run())
