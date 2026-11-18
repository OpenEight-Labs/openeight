"""Real stdio MCP sessions; OpenEight policy transport is simulated here."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from _policy_helpers import PolicyClient, call, config, model
from langchain_core.messages import AIMessage

from openeight import ConfigurationError
from openeight.langchain import _owner as implementation
from openeight.langchain import create_agent
from openeight.mcp import DiscoveryConfig, MCPSource, MCPTools
from openeight.mcp._source import tool_name

pytestmark = pytest.mark.asyncio


class AtomicState(type(Path())):
    def write_text(self, data, **kwargs):
        temporary = Path(str(self) + ".tmp")
        result = temporary.write_text(data, **kwargs)
        temporary.replace(self)
        return result


def make_source(tmp_path, monkeypatch):
    monkeypatch.setattr(implementation, "Client", PolicyClient)
    PolicyClient.instances = []
    state = AtomicState(tmp_path / "source.json")
    receipts = tmp_path / "receipts.jsonl"
    state.write_text(json.dumps({"tools": ["effect"]}))
    script = Path(__file__).parents[1] / "mcp" / "stdio_server.py"
    source = MCPSource.stdio(
        source_id="support",
        binding_id="principal-v1",
        command=sys.executable,
        args=[str(script), str(state), str(receipts)],
    )
    return source, state, receipts


@pytest.fixture
def source(tmp_path, monkeypatch):
    return make_source(tmp_path, monkeypatch)


def options(source, **kwargs):
    return config(
        sources=[source],
        discovery=DiscoveryConfig(refresh_interval=30, request_timeout=2),
        **kwargs,
    )


async def test_stdio_discovery_publication_and_single_authorized_effect(
    source, monkeypatch
):
    remote, state, receipts = source
    monkeypatch.setenv("AMBIENT_SDK_SECRET", "must-not-cross")
    name = tool_name(remote, "effect")
    async with create_agent(model(call(name)), openeight=options(remote)) as agent:
        await agent.aflush()
        assert not receipts.exists() and not PolicyClient.instances[0].writes
        rows = agent.catalog_snapshot().bindings
        assert rows[0]["name"] == name and rows[0]["remote_name"] == "effect"
        await agent.ainvoke({"messages": [("user", "go")]})
        assert len(PolicyClient.instances[0].requests) == 1
    writes = [json.loads(line) for line in receipts.read_text().splitlines()]
    assert writes == [
        {"name": "effect", "arguments": {"value": "original"}, "ambient_secret": None}
    ]


async def test_runtime_variants_share_discovery_refresh_and_actual_attempt_governance(
    source, monkeypatch
):
    from _external_control import ExternalControlPlane
    from langchain_core.runnables import ConfigurableField

    from openeight import Client, ModelVariant, RuntimeModelConfig, _adapter_http
    from openeight.langchain import ApprovalConfig

    remote, state, receipts = source
    name = tool_name(remote, "effect")
    service = ExternalControlPlane()
    service.write_names.add(name)
    monkeypatch.setattr(implementation, "Client", Client)
    monkeypatch.setattr(_adapter_http, "DeadlineHTTPTransport", lambda: service)
    first, second = model(call(name)), model(call(name))
    configurable = first.configurable_alternatives(
        ConfigurableField(id="choice"), default_key="first", second=second
    )
    runtime = RuntimeModelConfig(
        variants=(
            ModelVariant(name="first", configurable={"choice": "first"}),
            ModelVariant(name="second", configurable={"choice": "second"}),
        )
    )
    settings = {
        "agent_name": "support",
        "endpoint": "https://control.example",
        "credential": "ctk_test",
        "sources": [remote],
        "runtime_models": runtime,
        "approval": ApprovalConfig(
            timeout=0.01, poll_interval=0.001, poll_max_interval=0.002
        ),
    }
    configs = [{"configurable": {"choice": value}} for value in ("first", "second")]
    async with create_agent(configurable, openeight=settings) as agent:
        await agent.aflush()
        prepared = agent._session._prepared_models
        assert len(agent.catalog_snapshot().facts) == 1
        assert not any(
            operation == "WriteTelemetry" and body["runs"]
            for operation, body in service.requests
        )
        await agent.abatch([{"messages": [("user", "go")]}] * 2, configs)
        assert (
            len(receipts.read_text().splitlines()) == 2 and len(service.decisions) == 2
        )
        state.write_text(json.dumps({"tools": ["effect", "added"], "notify": False}))
        await agent.arefresh_tools()
        await agent.aflush()
        assert len(agent.catalog_snapshot().facts) == 2
        assert agent._session._prepared_models is prepared
        assert prepared["first"].model is first and prepared["second"].model is second
        for verdict, cfg in zip(("deny", "approval_required"), configs):
            service.verdict = verdict
            await agent.ainvoke({"messages": [("user", "blocked")]}, cfg)
        assert len(receipts.read_text().splitlines()) == 2
        assert len(service.decisions) == 4


@pytest.mark.parametrize("pending", [True, False])
async def test_pending_and_denied_mcp_never_dispatch(source, pending):
    remote, state, receipts = source
    name = tool_name(remote, "effect")
    async with create_agent(model(call(name)), openeight=options(remote)) as agent:
        client = PolicyClient.instances[0]
        client.pending = pending
        client.reject_name = name
        await agent.ainvoke({"messages": [("user", "go")]})
        assert not receipts.exists()


async def test_discovery_change_builds_next_generation_and_keeps_names_stable(source):
    remote, state, receipts = source
    async with create_agent(model(), openeight=options(remote)) as agent:
        first = agent.catalog_snapshot()
        state.write_text(json.dumps({"tools": ["effect", "second"]}))
        try:
            await agent.arefresh_tools()
        except ConfigurationError as error:
            assert error.reason == "tool_catalog_stale"
            await agent.arefresh_tools()
        second = agent.catalog_snapshot()
        assert len(second.facts) == 2
        assert first.facts[0].name in {fact.name for fact in second.facts}
        assert not receipts.exists()


async def test_direct_proxy_dispatch_is_rejected(source):
    remote, state, receipts = source
    async with create_agent(model(), openeight=options(remote)) as agent:
        proxy = next(iter(agent._session._owner.catalog.entries.values())).tool
        with pytest.raises(ConfigurationError):
            await proxy.ainvoke({"value": "bypass"})
        assert not receipts.exists()


async def test_child_selection_does_not_inherit_remote_tools(source):
    pytest.importorskip("deepagents")
    from openeight.deepagents import create_deep_agent

    remote, state, receipts = source
    name = tool_name(remote, "effect")
    child = dict(
        name="researcher", description="work", model=model(call(name)), tools=[]
    )
    async with create_deep_agent(
        model(call("task", description="work", subagent_type="researcher")),
        subagents=[child],
        openeight=options(remote),
    ) as agent:
        rows = [row for row in agent.catalog_snapshot().bindings if row["source_id"]]
        assert [row["child_path"] for row in rows] == [()]
        with pytest.raises(ConfigurationError):
            await agent.ainvoke({"messages": [("user", "go")]})
        assert not receipts.exists()


async def test_explicit_child_selection_authorizes_remote_effect(source):
    pytest.importorskip("deepagents")
    from openeight.deepagents import create_deep_agent

    remote, state, receipts = source
    name = tool_name(remote, "effect")
    child = dict(
        name="researcher", description="work", model=model(call(name)), tools=[]
    )
    async with create_deep_agent(
        model(call("task", description="work", subagent_type="researcher")),
        subagents=[child],
        openeight=options(
            remote, mcp_tools={("researcher",): [MCPTools("support", ("effect",))]}
        ),
    ) as agent:
        rows = [row for row in agent.catalog_snapshot().bindings if row["source_id"]]
        assert [row["child_path"] for row in rows] == [("researcher",)]
        await agent.ainvoke({"messages": [("user", "go")]})
        assert len(receipts.read_text().splitlines()) == 1


async def test_unknown_exact_selection_fails_entry_without_effect(source):
    remote, state, receipts = source
    with pytest.raises(ConfigurationError) as error:
        async with create_agent(
            model(),
            openeight=options(
                remote, mcp_tools={(): [MCPTools("support", ("missing",))]}
            ),
        ):
            pass
    assert error.value.reason == "mcp_tool_selection_missing"
    assert not receipts.exists() and not PolicyClient.instances


async def test_lost_response_does_not_retry_effect(source):
    remote, state, receipts = source
    state.write_text(json.dumps({"tools": ["effect"], "lose_response": True}))
    async with create_agent(
        model(call(tool_name(remote, "effect"))),
        openeight=config(
            sources=[remote], discovery=DiscoveryConfig(request_timeout=10)
        ),
    ) as agent:
        with pytest.raises(ConfigurationError):
            await agent.ainvoke({"messages": [("user", "go")]})
        assert len(receipts.read_text().splitlines()) == 1


async def test_http_discovery_notification_and_execution(source):
    import socket

    remote, state, receipts = source
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = await asyncio.create_subprocess_exec(
        remote.command,
        *remote.args,
        str(port),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env={},
    )
    try:
        for _ in range(100):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.02)
        else:
            pytest.fail("MCP HTTP fixture failed to start")
        remote = MCPSource.http(
            source_id=remote.source_id,
            binding_id=remote.binding_id,
            url=f"http://127.0.0.1:{port}/mcp/",
        )
        async with create_agent(
            model(call(tool_name(remote, "effect"))),
            openeight=config(
                sources=[remote], discovery=DiscoveryConfig(request_timeout=10)
            ),
        ) as agent:
            await agent.ainvoke({"messages": [("user", "go")]})
            assert len(receipts.read_text().splitlines()) == 1
            state.write_text(json.dumps({"tools": ["effect", "second"]}))
            for _ in range(100):
                if len(agent.catalog_snapshot().facts) == 2:
                    break
                await asyncio.sleep(0.02)
            assert len(agent.catalog_snapshot().facts) == 2
            state.write_text(
                json.dumps(
                    {
                        "tools": ["effect", "second"],
                        "notify": False,
                        "hold_response": True,
                    }
                )
            )
            pending = asyncio.create_task(
                agent.ainvoke({"messages": [("user", "again")]})
            )
            for _ in range(500):
                if len(receipts.read_text().splitlines()) == 2:
                    break
                if pending.done():
                    await pending
                    pytest.fail("held remote call completed before its receipt")
                await asyncio.sleep(0.01)
            source_state = agent._session._coordinator.sources["support"]
            old_session, old_epoch = source_state.session, source_state.epoch
            assert source_state.inflight == 1
            state.write_text(
                json.dumps(
                    {
                        "tools": ["effect", "second"],
                        "notify": False,
                        "hold_response": True,
                        "drop_get": True,
                    }
                )
            )
            for _ in range(100):
                if source_state.failed:
                    break
                await asyncio.sleep(0.01)
            assert (
                source_state.failed
                and source_state.session is old_session
                and not pending.done()
            )
            state.write_text(
                json.dumps({"tools": ["effect", "second"], "notify": False})
            )
            await pending
            await agent.arefresh_tools()
            assert source_state.epoch != old_epoch
            assert len(receipts.read_text().splitlines()) == 2
            state.write_text(
                json.dumps(
                    {"tools": ["effect", "second"], "notify": False, "close_delay": 0.5}
                )
            )
            import time

            started = time.monotonic()
            from openeight import CleanupIncompleteError

            with pytest.raises(CleanupIncompleteError):
                await agent.aclose(0.05)
            status = agent.delivery_status()
            assert time.monotonic() - started < 0.25 and not status.closed
            assert not agent.delivery_status().closed
    finally:
        if process.returncode is None:
            process.terminate()
        await asyncio.wait_for(process.wait(), 3)


async def test_removed_binding_during_decide_executes_zero_remote_calls(
    source, monkeypatch
):
    import threading

    remote, state, receipts = source
    # Explicit refresh owns the binding change; notifications are tested separately.
    state.write_text(json.dumps({"tools": ["effect"], "notify": False}))
    started, release = threading.Event(), threading.Event()
    barrier_timeout = 10
    async with create_agent(
        model(call(tool_name(remote, "effect"))),
        openeight=config(
            sources=[remote],
            discovery=DiscoveryConfig(request_timeout=barrier_timeout),
        ),
    ) as agent:
        client = PolicyClient.instances[0]
        original = client.decide

        def decide(**request):
            result = original(**request)
            started.set()
            assert release.wait(barrier_timeout), "Decision barrier was not released"
            return result

        monkeypatch.setattr(client, "decide", decide)
        invocation = asyncio.create_task(agent.ainvoke({"messages": [("user", "go")]}))
        try:
            assert await asyncio.to_thread(started.wait, barrier_timeout), (
                "Invocation did not reach the Decision barrier"
            )
            state.write_text(json.dumps({"tools": [], "notify": False}))
            await agent.arefresh_tools()
            assert not invocation.done()
            release.set()
            with pytest.raises(ConfigurationError) as error:
                await invocation
            assert error.value.reason == "tool_binding_changed"
            assert not receipts.exists()
        finally:
            release.set()
            if not invocation.done():
                invocation.cancel()
            await asyncio.gather(invocation, return_exceptions=True)


async def test_remove_reappear_revokes_old_binding_permanently(source):
    remote, state, receipts = source
    # Explicit refreshes isolate binding retirement from asynchronous notifications.
    state.write_text(json.dumps({"tools": ["effect"], "notify": False}))
    async with create_agent(model(), openeight=options(remote)) as agent:
        original = next(iter(agent._session._owner.catalog.entries.values())).remote
        state.write_text(json.dumps({"tools": [], "notify": False}))
        await agent.arefresh_tools()
        state.write_text(json.dumps({"tools": ["effect"], "notify": False}))
        await agent.arefresh_tools()
        replacement = next(iter(agent._session._owner.catalog.entries.values())).remote
        assert replacement is not original and original.revoked
        assert replacement.fact.name == original.fact.name
        assert not receipts.exists()


async def test_discovery_limits_apply_before_empty_selection(source):
    remote, state, receipts = source
    state.write_text(json.dumps({"tools": ["effect", "second"]}))
    governance = config(
        sources=[remote], mcp_tools={(): []}, discovery=DiscoveryConfig(max_tools=1)
    )
    with pytest.raises(ConfigurationError) as error:
        async with create_agent(model(), openeight=governance):
            pass
    assert error.value.reason == "tool_discovery_limit_exceeded"
    assert not receipts.exists() and not PolicyClient.instances


async def test_reconnect_keeps_admitted_session_until_call_settles(source):
    remote, state, receipts = source
    state.write_text(
        json.dumps({"tools": ["effect"], "hold_response": True, "notify": False})
    )
    async with create_agent(
        model(call(tool_name(remote, "effect"))),
        openeight=config(
            sources=[remote], discovery=DiscoveryConfig(request_timeout=10)
        ),
    ) as agent:
        invocation = asyncio.create_task(agent.ainvoke({"messages": [("user", "go")]}))
        for _ in range(100):
            if receipts.exists():
                break
            await asyncio.sleep(0.01)
        source_state = agent._session._coordinator.sources["support"]
        old_session = source_state.session
        old_epoch = source_state.epoch
        assert source_state.inflight == 1
        source_state.disconnect()
        with pytest.raises(ConfigurationError):
            await agent.arefresh_tools()
        assert source_state.session is old_session and not invocation.done()
        state.write_text(json.dumps({"tools": ["effect"], "notify": False}))
        await invocation
        assert source_state.inflight == 0
        await agent.arefresh_tools()
        assert source_state.epoch != old_epoch
        assert len(receipts.read_text().splitlines()) == 1


async def test_unselected_addition_enforces_combined_budget(source):
    from langchain_core.tools import tool

    @tool
    def local(value: str) -> str:
        """Local tool also consumes the catalog budget."""
        return value

    remote, state, receipts = source
    governance = config(
        sources=[remote],
        mcp_tools={(): [MCPTools("support", ("effect",))]},
        discovery=DiscoveryConfig(max_tools=2),
    )
    async with create_agent(model(), [local], openeight=governance) as agent:
        first = agent.catalog_snapshot()
        state.write_text(
            json.dumps({"tools": ["effect", "unselected"], "notify": False})
        )
        with pytest.raises(
            ConfigurationError, match="Tool discovery limit is exceeded"
        ):
            await agent.arefresh_tools()
        assert agent.catalog_snapshot().generation == first.generation
        assert agent.catalog_status().sources[0]["state"] == "stale"
        assert not receipts.exists()


async def test_empty_listing_is_complete_and_readiness_is_generation_bound(source):
    remote, state, receipts = source
    state.write_text(json.dumps({"tools": []}))
    async with create_agent(model(), openeight=options(remote)) as agent:
        await agent.aflush()
        snapshot = agent.catalog_snapshot()
        assert snapshot.facts == () and snapshot.publication_complete
        assert agent.catalog_status().sources[0]["state"] == "empty"
        readiness = await agent.areadiness()
        assert (
            readiness.generation == snapshot.generation and readiness.unpublished == ()
        )
        assert not receipts.exists()


async def test_http_stream_bounds_json_and_split_sse_delimiters():
    import httpx

    from openeight.mcp._transport import _Stream

    class Bytes(httpx.AsyncByteStream):
        def __init__(self, chunks):
            self.chunks = chunks

        async def __aiter__(self):
            for chunk in self.chunks:
                yield chunk

    invalidations = []
    for sse, chunks in [
        (False, [b"a" * 1048576, b"b"]),
        (True, [b"data:" + b"a" * 600000 + b"\r", b"\ndata:" + b"a" * 600000]),
    ]:
        with pytest.raises(ConfigurationError, match="Tool source is unavailable"):
            async for _ in _Stream(
                Bytes(chunks), lambda: invalidations.append(True), False, sse=sse
            ):
                pass
    assert len(invalidations) == 2
    chunks = [
        b"data:" + b"a" * 600000 + b"\r\n\r",
        b"\ndata:" + b"a" * 600000 + b"\n\n",
    ]
    assert (
        len(
            [
                chunk
                async for chunk in _Stream(
                    Bytes(chunks), lambda: invalidations.append(True), False, sse=True
                )
            ]
        )
        == 2
    )
    assert len(invalidations) == 2


@pytest.mark.parametrize(
    "flag,reason",
    [
        ("duplicate", "unsupported_tool_definition"),
        ("repeat_cursor", "unsupported_tool_definition"),
        ("list_failure", "tool_source_unavailable"),
        ("notify_during_list", "tool_catalog_stale"),
    ],
)
async def test_invalid_refresh_preserves_generation_and_blocks_remote(
    source, flag, reason
):
    remote, state, receipts = source
    async with create_agent(model(), openeight=options(remote)) as agent:
        before = agent.catalog_snapshot()
        state.write_text(json.dumps({"tools": ["effect"], "notify": False, flag: True}))
        with pytest.raises(ConfigurationError) as error:
            await agent.arefresh_tools()
        assert error.value.reason == reason
        assert agent.catalog_snapshot().generation == before.generation
        binding = agent._session._coordinator.bindings["support", "effect"]
        with pytest.raises(ConfigurationError):
            binding.validate()
        assert not receipts.exists()


async def test_freshness_expiry_during_decision_has_zero_remote_effects(source):
    remote, state, receipts = source
    name = tool_name(remote, "effect")
    async with create_agent(model(call(name)), openeight=options(remote)) as agent:
        client = PolicyClient.instances[0]
        original = client.decide

        def expire(**request):
            agent._session._coordinator.sources["support"].fresh_until = 0
            return original(**request)

        client.decide = expire
        with pytest.raises(ConfigurationError) as error:
            await agent.ainvoke({"messages": [("user", "go")]})
        assert error.value.reason == "tool_catalog_stale"
        assert not receipts.exists()


async def test_generation_capacity_releases_only_after_owned_attempts_settle(source):
    from langchain_core.messages import ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import tool
    from test_framework_boundary import ToolModel

    started, release = asyncio.Queue(), asyncio.Event()

    @tool
    async def hold(value: str) -> str:
        """Hold an admitted local effect while catalogs change."""
        await started.put(value)
        await release.wait()
        return value

    class RepeatModel(ToolModel):
        def _generate(self, messages, **kwargs):
            message = (
                AIMessage(content="done")
                if isinstance(messages[-1], ToolMessage)
                else call("hold")
            )
            return ChatResult(generations=[ChatGeneration(message=message)])

        async def _agenerate(self, messages, **kwargs):
            return self._generate(messages)

    remote, state, receipts = source
    governance = config(sources=[remote], discovery=DiscoveryConfig(max_generations=2))
    async with create_agent(
        RepeatModel(responses=[]), [hold], openeight=governance
    ) as agent:
        tasks = []
        try:
            for names in [["effect"], ["effect", "second"]]:
                state.write_text(json.dumps({"tools": names, "notify": False}))
                await agent.arefresh_tools()
                tasks.append(
                    asyncio.create_task(agent.ainvoke({"messages": [("user", "go")]}))
                )
                await asyncio.wait_for(started.get(), 2)
            state.write_text(
                json.dumps({"tools": ["effect", "second", "third"], "notify": False})
            )
            with pytest.raises(ConfigurationError) as error:
                await agent.arefresh_tools()
            assert error.value.reason == "execution_busy"
            assert len(agent._session._coordinator.generations) == 2
        finally:
            release.set()
            await asyncio.gather(*tasks)
        await agent.arefresh_tools()
        await asyncio.sleep(0)
        assert len(agent._session._coordinator.generations) == 1
        assert len(agent.catalog_snapshot().facts) == 4
        assert not receipts.exists()


@pytest.mark.parametrize(
    "delimiter", [b"\n\r\n", b"\n\r", b"\r\n\n", b"\r\r", b"\r\n\r\n"]
)
async def test_http_sse_mixed_event_delimiters_across_chunks(delimiter):
    import httpx

    from openeight.mcp._transport import _Stream

    class Bytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"data:" + b"a" * 600000
            for byte in delimiter:
                yield bytes([byte])
            yield b"data:" + b"b" * 600000 + b"\n\n"

    invalidations = []
    chunks = [
        chunk
        async for chunk in _Stream(
            Bytes(), lambda: invalidations.append(True), False, sse=True
        )
    ]
    assert chunks and not invalidations


async def test_compressed_http_response_rejects_before_decoding():
    import gzip

    import httpx

    from openeight.mcp._transport import _HTTPTransport

    invalidations = []
    transport = _HTTPTransport(lambda: invalidations.append(True))
    await transport.base.aclose()
    transport.base = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            content=gzip.compress(b"a" * 2000000),
        )
    )
    with pytest.raises(ConfigurationError) as error:
        await transport.handle_async_request(
            httpx.Request("POST", "https://tools.example/mcp")
        )
    assert error.value.reason == "tool_source_unavailable" and invalidations == [True]
    await transport.aclose()


@pytest.mark.parametrize("failure", ["transport", "unavailable", "protocol"])
async def test_publication_outage_keeps_background_discovery_alive(source, failure):
    from openeight import APIError, ProtocolError, TransportError

    remote, state, receipts = source
    state.write_text(json.dumps({"tools": ["effect"], "notify": False}))
    governance = config(
        sources=[remote],
        discovery=DiscoveryConfig(refresh_interval=0.2, request_timeout=2),
    )

    async def eventually(predicate):
        async with asyncio.timeout(10):
            while not predicate():
                await asyncio.sleep(0.01)

    async with create_agent(model(), openeight=governance) as agent:
        await agent.aflush()
        coordinator = agent._session._coordinator
        first = coordinator.current
        client = PolicyClient.instances[0]
        register = client.register_tools
        attempts = []
        unavailable = True

        def publish(facts, **kwargs):
            attempts.append(facts)
            if unavailable:
                if failure == "transport":
                    raise TransportError()
                if failure == "protocol":
                    raise ProtocolError(200)
                raise APIError(503, "unavailable", "unavailable")
            return register(facts, **kwargs)

        client.register_tools = publish
        state.write_text(json.dumps({"tools": ["effect", "second"], "notify": False}))
        coordinator.changed.set()
        await eventually(
            lambda: coordinator.error is not None or coordinator.task.done()
        )
        assert not coordinator.task.done(), (
            "publication failure must not kill discovery"
        )
        assert coordinator.error == "tool_publication_unavailable"
        assert coordinator.current is first
        assert not coordinator.sources["support"].available
        assert not receipts.exists()

        unavailable = False
        await eventually(lambda: coordinator.current is not first)
        assert attempts[0] == attempts[1], (
            "retry must preserve the original CAS conditions"
        )
        assert len(agent.catalog_snapshot().facts) == 2
        recovered = coordinator.current
        state.write_text(
            json.dumps({"tools": ["effect", "second", "third"], "notify": False})
        )
        await eventually(lambda: coordinator.current is not recovered)
        assert len(agent.catalog_snapshot().facts) == 3
        assert not coordinator.task.done()
        assert not receipts.exists()


@pytest.mark.parametrize("mode", ["none", "inputs", "outputs", "all"])
async def test_mcp_results_follow_invocation_capture_policy(source, mode):
    remote, state, receipts = source
    calls = []

    def redact(ctx, value):
        calls.append((ctx.kind, ctx.direction))
        return "[REDACTED]"

    name = tool_name(remote, "effect")
    async with create_agent(
        model(call(name, value="secret-marker")),
        openeight=options(
            remote,
            capture_content=mode,
            redact_content=redact,
        ),
    ) as agent:
        await agent.ainvoke({"messages": [("user", "secret-marker")]})
        await agent.aflush()
        events = [
            e
            for body in PolicyClient.instances[0].writes
            for e in body.get("events", [])
        ]
        (terminal,) = [
            e for e in events if e.get("tool_name") == name and e.get("decision_id")
        ]
        assert "secret-marker" in terminal["input_json"]  # required governance evidence
        assert ("output_json" in terminal) == (mode in ("outputs", "all"))
        assert not any(
            kind == "tool_call" and direction == "input" for kind, direction in calls
        )
    assert json.loads(receipts.read_text().splitlines()[0])["arguments"] == {
        "value": "secret-marker"
    }
