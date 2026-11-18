"""Deterministic Deep Agents construction probes; not a production SDK adapter."""

import json
from uuid import uuid4

import pytest

pytest.importorskip("deepagents", reason="Native probes require the deepagents extra")
pytest.importorskip(
    "langgraph.checkpoint.sqlite.aio",
    reason="Native probes require the deepagents-durable extra",
)

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from deepagents.middleware.skills import SkillsMiddleware, SkillsState
from langchain.agents.middleware import AgentMiddleware, HumanInTheLoopMiddleware
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

pytestmark = pytest.mark.asyncio


class ScriptModel(FakeMessagesListChatModel):
    prompts: list = []

    def _generate(self, messages, *args, **kwargs):
        self.prompts.append(messages)
        return super()._generate(messages, *args, **kwargs)

    def bind_tools(self, tools, **kwargs):
        return self


def call(name, args, id="call"):
    return AIMessage(content="", tool_calls=[dict(name=name, args=args, id=id)])


def model(*messages):
    return ScriptModel(responses=[*messages, AIMessage(content="done")])


class Gate(AgentMiddleware):
    def __init__(self, seen, denied=(), label="root"):
        self.seen, self.denied, self.label = seen, set(denied), label

    def before_agent(self, state, runtime):
        pass

    async def abefore_agent(self, state, runtime):
        pass

    async def awrap_tool_call(self, request, handler):
        self.seen.append(
            (
                self.label,
                request.tool_call["name"],
                dict(request.tool_call["args"]),
                request.tool is not None,
            )
        )
        if request.tool_call["name"] in self.denied:
            return ToolMessage(
                content="denied", status="error", tool_call_id=request.tool_call["id"]
            )
        return await handler(request)


class GovernedSkills(SkillsMiddleware):
    """Probe public middleware replacement, retaining upstream skills behavior."""

    @property
    def name(self):
        return "SkillsMiddleware"

    def __init__(self, gate, *, sources=(), backend=None):
        super().__init__(backend=backend or StateBackend(), sources=list(sources))
        self.gate = gate

    async def awrap_tool_call(self, request, handler):
        async def authorize(resolved):
            return await self.gate.awrap_tool_call(resolved, handler)

        return await super().awrap_tool_call(request, authorize)


def effect_tool(effects):
    @tool
    async def effect(value: str) -> str:
        """Record one effect."""
        effects.append(value)
        return value

    return effect


def registry(graph):
    return graph.nodes["tools"].bound.tools_by_name


def wrapper_names(graph):
    def unpack(f):
        if getattr(f, "__self__", None):
            return [type(f.__self__).__name__]
        cv = dict(
            zip(
                getattr(getattr(f, "__code__", None), "co_freevars", ()),
                [c.cell_contents for c in (getattr(f, "__closure__", None) or ())],
            )
        )
        if "outer" in cv and "inner" in cv:
            return unpack(cv["outer"]) + unpack(cv["inner"])
        if "func" in cv:
            return unpack(cv["func"])
        return [str(cv.keys())]

    return unpack(graph.nodes["tools"].bound._awrap_tool_call)


async def test_capture_generated_tools_without_execution():
    effects = []
    seen = []
    g = create_deep_agent(
        model(), tools=[effect_tool(effects)], middleware=[Gate(seen)]
    )
    assert {
        "effect",
        "task",
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
    } <= set(registry(g))
    assert not effects and not seen


@pytest.mark.parametrize("denied", [False, True])
async def test_root_effect_gate(denied):
    effects = []
    seen = []
    g = create_deep_agent(
        model(call("effect", {"value": "root"})),
        tools=[effect_tool(effects)],
        middleware=[Gate(seen, ["effect"] if denied else [])],
    )
    await g.ainvoke({"messages": [("user", "go")]})
    assert effects == ([] if denied else ["root"])
    assert [x[1] for x in seen] == ["effect"]


@pytest.mark.parametrize("deny_write", [False, True])
async def test_generated_filesystem_tool_gate(deny_write):
    seen = []
    g = create_deep_agent(
        model(call("write_file", {"file_path": "/probe.txt", "content": "sentinel"})),
        middleware=[Gate(seen, ["write_file"] if deny_write else [])],
    )
    r = await g.ainvoke({"messages": [("user", "go")]})
    assert ("/probe.txt" in r.get("files", {})) is (not deny_write)
    assert [x[1] for x in seen] == ["write_file"]


async def test_parent_gate_does_not_cover_default_child_counterexample():
    effects = []
    seen = []
    m = model(
        call(
            "task",
            {"description": "do effect", "subagent_type": "general-purpose"},
            "task",
        ),
        call("effect", {"value": "child"}, "child"),
        AIMessage(content="child done"),
    )
    g = create_deep_agent(
        m, tools=[effect_tool(effects)], middleware=[Gate(seen, ["effect"])]
    )
    await g.ainvoke({"messages": [("user", "go")]})
    assert effects == ["child"]
    assert [x[1] for x in seen] == ["task"]


@pytest.mark.parametrize("child_name", ["researcher", "general-purpose"])
@pytest.mark.parametrize("denied", [False, True])
async def test_explicit_child_gate(child_name, denied):
    effects = []
    seen = []
    child = {
        "name": child_name,
        "description": "do work",
        "system_prompt": "work",
        "model": model(call("effect", {"value": "child"})),
        "tools": [effect_tool(effects)],
        "middleware": [Gate(seen, ["effect"] if denied else [], child_name)],
    }
    g = create_deep_agent(
        model(call("task", {"description": "go", "subagent_type": child_name})),
        middleware=[Gate(seen)],
        subagents=[child],
    )
    await g.ainvoke({"messages": [("user", "go")]})
    assert effects == ([] if denied else ["child"])
    assert [(x[0], x[1]) for x in seen] == [("root", "task"), (child_name, "effect")]


async def test_plain_skills_is_inner_than_user_gate():
    seen = []
    g = create_deep_agent(model(), skills=["/skills/"], middleware=[Gate(seen)])
    names = wrapper_names(g)
    assert names.index("Gate") < names.index("SkillsMiddleware"), names


@pytest.mark.parametrize("denied", [False, True])
async def test_public_skills_replacement_gates_default_child(denied):
    effects = []
    seen = []
    m = model(
        call("task", {"description": "go", "subagent_type": "general-purpose"}, "task"),
        call("effect", {"value": "child"}, "child"),
        AIMessage(content="child done"),
    )
    gate = Gate(seen, ["effect"] if denied else [])
    g = create_deep_agent(
        m, tools=[effect_tool(effects)], middleware=[GovernedSkills(gate)]
    )
    await g.ainvoke({"messages": [("user", "go")]})
    assert effects == ([] if denied else ["child"])
    assert [x[1] for x in seen] == ["task", "effect"]


@pytest.mark.parametrize("skills", [False, True])
async def test_skills_replacement_final_tool_wrapper(skills):
    g = create_deep_agent(
        model(),
        middleware=[GovernedSkills(Gate([]), sources=["/skills/"] if skills else [])],
    )
    assert wrapper_names(g)[-1] == "GovernedSkills", wrapper_names(g)


async def test_native_hitl_edit_after_skills_gate_counterexample(tmp_path):
    effects = []
    seen = []
    async with AsyncSqliteSaver.from_conn_string(
        str(tmp_path / "checkpoint.db")
    ) as saver:
        g = create_deep_agent(
            model(call("effect", {"value": "original"})),
            tools=[effect_tool(effects)],
            middleware=[GovernedSkills(Gate(seen))],
            interrupt_on={"effect": True},
            checkpointer=saver,
        )
        cfg = {"configurable": {"thread_id": "test-thread"}}
        r = await g.ainvoke({"messages": [("user", "go")]}, cfg)
        assert r.get("__interrupt__") and not effects and not seen
        await g.ainvoke(
            Command(
                resume={
                    "decisions": [
                        {
                            "type": "edit",
                            "edited_action": {
                                "name": "effect",
                                "args": {"value": "edited"},
                            },
                        }
                    ]
                }
            ),
            cfg,
        )
        assert effects == ["edited"]
        assert seen[0][2] == {
            "value": "original"
        }  # Gate would authorize different bytes!


async def test_raw_debug_prints_state_counterexample(capsys):
    g = create_deep_agent(model(), debug=True)
    await g.ainvoke({"messages": [("user", "SECRET_SENTINEL_923")]})
    assert "SECRET_SENTINEL_923" in capsys.readouterr().out


async def test_debug_false_does_not_print_state(capsys):
    g = create_deep_agent(model(), debug=False)
    await g.ainvoke({"messages": [("user", "SECRET_SENTINEL_923")]})
    assert "SECRET_SENTINEL_923" not in capsys.readouterr().out


@pytest.mark.parametrize("allowed_child", [False, True])
async def test_explicit_mcp_shaped_proxy_selection(allowed_child):
    # In-memory proxy-shaped tool, not an MCP transport test.
    effects = []
    seen = []
    remote = effect_tool(effects)
    child = {
        "name": "researcher",
        "description": "do work",
        "system_prompt": "work",
        "model": model(call("effect", {"value": "remote"})),
        "tools": [remote] if allowed_child else [],
        "middleware": [Gate(seen, label="researcher")],
    }
    g = create_deep_agent(
        model(call("task", {"description": "go", "subagent_type": "researcher"})),
        tools=[],
        subagents=[child],
        middleware=[Gate(seen)],
    )
    await g.ainvoke({"messages": [("user", "go")]})
    assert effects == (["remote"] if allowed_child else [])


class CombinedState(SkillsState, HumanInTheLoopMiddleware.state_schema):
    pass


class SkillsApprovalGate(GovernedSkills):
    """Candidate composition of public hooks, preserving HITL before final gate."""

    state_schema = CombinedState

    def __init__(self, gate, *, interrupt_on=None, **kwargs):
        super().__init__(gate, **kwargs)
        self.approvals = HumanInTheLoopMiddleware(interrupt_on=interrupt_on or {})

    async def aafter_model(self, state, runtime):
        return await self.approvals.aafter_model(state, runtime)

    def after_model(self, state, runtime):
        return self.approvals.after_model(state, runtime)

    async def awrap_tool_call(self, request, handler):
        async def after_skill(resolved):
            async def after_edit(edited):
                return await self.gate.awrap_tool_call(edited, handler)

            return await self.approvals.awrap_tool_call(resolved, after_edit)

        return await SkillsMiddleware.awrap_tool_call(self, request, after_skill)


@pytest.mark.parametrize("child", [False, "researcher", "general-purpose"])
@pytest.mark.parametrize("denied", [False, True])
async def test_composed_public_hooks_authorize_edited_bytes(tmp_path, child, denied):
    effects = []
    seen = []
    boundary = SkillsApprovalGate(
        Gate(seen, ["effect"] if denied else [], "child" if child else "root"),
        interrupt_on={"effect": True},
    )
    async with AsyncSqliteSaver.from_conn_string(
        str(tmp_path / "checkpoint.db")
    ) as saver:
        if child:
            spec = {
                "name": child,
                "description": "work",
                "system_prompt": "work",
                "model": model(call("effect", {"value": "original"})),
                "tools": [effect_tool(effects)],
                "middleware": [boundary],
                "interrupt_on": None,
            }
            g = create_deep_agent(
                model(call("task", {"description": "go", "subagent_type": child})),
                middleware=[SkillsApprovalGate(Gate(seen))],
                subagents=[spec],
                checkpointer=saver,
            )
        else:
            g = create_deep_agent(
                model(call("effect", {"value": "original"})),
                tools=[effect_tool(effects)],
                middleware=[boundary],
                checkpointer=saver,
            )
        cfg = {"configurable": {"thread_id": "thread"}}
        r = await g.ainvoke({"messages": [("user", "go")]}, cfg)
        assert r.get("__interrupt__") and not effects
        await g.ainvoke(
            Command(
                resume={
                    "decisions": [
                        {
                            "type": "edit",
                            "edited_action": {
                                "name": "effect",
                                "args": {"value": "edited"},
                            },
                        }
                    ]
                }
            ),
            cfg,
        )
        assert effects == ([] if denied else ["edited"])
        attempts = [x for x in seen if x[1] == "effect"]
        assert len(attempts) == 1 and attempts[0][2] == {"value": "edited"}


class RecordingSaver(AsyncSqliteSaver):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.accesses = []

    async def aget_tuple(self, config):
        self.accesses.append(("read", dict(config.get("configurable", {}))))
        return await super().aget_tuple(config)

    async def aput(self, config, *args, **kwargs):
        self.accesses.append(("write", dict(config.get("configurable", {}))))
        return await super().aput(config, *args, **kwargs)

    async def aput_writes(self, config, *args, **kwargs):
        self.accesses.append(("writes", dict(config.get("configurable", {}))))
        return await super().aput_writes(config, *args, **kwargs)


async def test_delegated_checkpoint_uses_supplied_saver(tmp_path):
    effects = []
    seen = []
    async with RecordingSaver.from_conn_string(
        str(tmp_path / "checkpoint.db")
    ) as saver:
        child = {
            "name": "researcher",
            "description": "work",
            "system_prompt": "work",
            "model": model(call("effect", {"value": "child"})),
            "tools": [effect_tool(effects)],
            "middleware": [Gate(seen, label="child")],
        }
        g = create_deep_agent(
            model(call("task", {"description": "go", "subagent_type": "researcher"})),
            subagents=[child],
            checkpointer=saver,
        )
        await g.ainvoke(
            {"messages": [("user", "go")]},
            {"configurable": {"thread_id": "owner-thread"}},
        )
        assert effects == ["child"]
        assert saver.accesses
        assert all(x[1]["thread_id"] == "owner-thread" for x in saver.accesses)
        namespaces = {x[1].get("checkpoint_ns", "") for x in saver.accesses}
        assert "" in namespaces and any(ns for ns in namespaces), namespaces
        (tmp_path / "checkpoint_namespaces.json").write_text(
            json.dumps(sorted(namespaces))
        )


class RewriteRetry(AgentMiddleware):
    async def awrap_tool_call(self, request, handler):
        edited = request.override(
            tool_call={**request.tool_call, "args": {"value": "changed"}}
        )
        await handler(edited)
        return await handler(edited)


async def test_composed_gate_sees_each_retry_final_args():
    effects = []
    seen = []
    g = create_deep_agent(
        model(call("effect", {"value": "original"})),
        tools=[effect_tool(effects)],
        middleware=[RewriteRetry(), SkillsApprovalGate(Gate(seen))],
    )
    await g.ainvoke({"messages": [("user", "go")]})
    assert effects == ["changed", "changed"]
    assert len(seen) == 2 and all(x[2] == {"value": "changed"} for x in seen)


async def test_composed_gate_preserves_plain_skill_loading():
    seen = []
    m = model()
    g = create_deep_agent(
        m, middleware=[SkillsApprovalGate(Gate(seen), sources=["/skills/"])]
    )
    data = {
        "messages": [("user", "go")],
        "files": {
            "/skills/example/SKILL.md": {
                "content": [
                    "---",
                    "name: example",
                    "description: Example skill",
                    "---",
                    "Use the local tools.",
                ],
                "created_at": "2026-10-08T00:00:00Z",
                "modified_at": "2026-10-08T00:00:00Z",
            }
        },
    }
    await g.ainvoke(data)
    assert "Example skill" in str(m.prompts), str(m.prompts)
    assert not seen


async def test_current_sdk_inspector_rejects_skills_after_gate():
    from openeight import ConfigurationError
    from openeight.langchain._compat import inspect_agent

    gate = Gate([])
    g = create_deep_agent(model(), middleware=[gate], skills=["/skills/"])
    with pytest.raises(ConfigurationError):
        inspect_agent(g, gate)


async def test_current_sdk_inspector_accepts_plain_root_but_not_child_coverage():
    from openeight.langchain._compat import inspect_agent

    gate = Gate([])
    g = create_deep_agent(model(), middleware=[gate])
    cap = inspect_agent(g, gate)
    assert cap.async_middleware_binding
    assert "task" in {t.name for t in cap.tools}
    # Root acceptance does not inspect the separately compiled default child.


async def test_global_debug_bypasses_graph_debug_false_counterexample(capsys):
    from langchain_core.globals import get_debug, set_debug

    previous = get_debug()
    try:
        set_debug(True)
        g = create_deep_agent(model(), debug=False)
        await g.ainvoke({"messages": [("user", "GLOBAL_DEBUG_SENTINEL_527")]})
        assert "GLOBAL_DEBUG_SENTINEL_527" in capsys.readouterr().out
    finally:
        set_debug(previous)


class ScopedSaverProbe(BaseCheckpointSaver):
    """Feasibility-only saver decorator; production admission/auth is not implemented."""

    def __init__(self, base):
        super().__init__(serde=base.serde)
        self.base = base
        self.prefix = str(uuid4()) + "/"
        self.threads = set()

    def new_thread(self):
        thread = str(uuid4())
        self.threads.add(thread)
        return thread

    def encode(self, config):
        selectors = dict(config["configurable"])
        if selectors["thread_id"] not in self.threads:
            raise ValueError("checkpoint_thread_unowned")
        selectors["thread_id"] = self.prefix + selectors["thread_id"]
        return {**config, "configurable": selectors}

    def decode(self, config):
        if config is None:
            return None
        selectors = dict(config["configurable"])
        assert selectors["thread_id"].startswith(self.prefix)
        selectors["thread_id"] = selectors["thread_id"][len(self.prefix) :]
        return {**config, "configurable": selectors}

    async def aget_tuple(self, config):
        result = await self.base.aget_tuple(self.encode(config))
        if result is None:
            return None
        return result._replace(
            config=self.decode(result.config),
            parent_config=self.decode(result.parent_config),
        )

    async def aput(self, config, *args, **kwargs):
        return self.decode(await self.base.aput(self.encode(config), *args, **kwargs))

    async def aput_writes(self, config, *args, **kwargs):
        return await self.base.aput_writes(self.encode(config), *args, **kwargs)

    def get_next_version(self, current, channel):
        return self.base.get_next_version(current, channel)

    async def aget_delta_channel_history(self, config, *args, **kwargs):
        return await self.base.aget_delta_channel_history(
            config=self.encode(config), *args, **kwargs
        )


async def test_scoped_saver_retains_child_namespaces_and_resume(tmp_path):
    effects = []
    seen = []
    async with RecordingSaver.from_conn_string(str(tmp_path / "checkpoint.db")) as base:
        saver = ScopedSaverProbe(base)
        thread = saver.new_thread()
        child = {
            "name": "researcher",
            "description": "work",
            "system_prompt": "work",
            "model": model(call("effect", {"value": "original"})),
            "tools": [effect_tool(effects)],
            "middleware": [
                SkillsApprovalGate(Gate(seen), interrupt_on={"effect": True})
            ],
        }
        g = create_deep_agent(
            model(call("task", {"description": "go", "subagent_type": "researcher"})),
            subagents=[child],
            checkpointer=saver,
        )
        config = {"configurable": {"thread_id": thread}}
        r = await g.ainvoke({"messages": [("user", "go")]}, config)
        assert r.get("__interrupt__") and not effects
        await g.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
        assert effects == ["original"]
        assert all(x[1]["thread_id"] == saver.prefix + thread for x in base.accesses)
        assert any(x[1].get("checkpoint_ns") for x in base.accesses)
        reads = len(base.accesses)
        replacement = ScopedSaverProbe(base)
        with pytest.raises(ValueError, match="checkpoint_thread_unowned"):
            await replacement.aget_tuple(config)
        assert len(base.accesses) == reads


async def test_scoped_saver_rejects_foreign_thread_before_storage(tmp_path):
    async with RecordingSaver.from_conn_string(str(tmp_path / "checkpoint.db")) as base:
        first = ScopedSaverProbe(base)
        second = ScopedSaverProbe(base)
        thread = first.new_thread()
        with pytest.raises(ValueError, match="checkpoint_thread_unowned"):
            await second.aget_tuple({"configurable": {"thread_id": thread}})
        assert not base.accesses
