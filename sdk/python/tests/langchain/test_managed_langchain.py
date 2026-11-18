"""Managed constructor behavior across the recorded LangChain matrix."""

import asyncio

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from test_enforcement_facade import GovernedClient
from test_framework_boundary import ToolModel

from openeight import ConfigurationError, OpenEightConfig
from openeight.langchain import _owner, _qualified, create_agent, observe


def config():
    return OpenEightConfig(
        agent_name="test", endpoint="http://localhost:7777", credential="ctk_secret"
    )


@pytest.mark.parametrize("observed", [False, True])
def test_public_constructor_owns_catalog_attempt_and_shutdown(monkeypatch, observed):
    client = GovernedClient()

    def client_factory(*args, **kwargs):
        assert kwargs["integration"] == "langchain"
        return client

    monkeypatch.setattr(_owner, "Client", client_factory)
    effects = []

    @tool
    async def effect(value: str) -> str:
        """Record an effect."""
        effects.append(value)
        return value

    model = ToolModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[dict(name="effect", args={"value": "ok"}, id="call")],
            ),
            AIMessage(content="done"),
        ]
    )

    async def run():
        context = (
            observe(
                model,
                [effect],
                openeight={
                    "agent_name": "test",
                    "endpoint": "http://localhost:7777",
                    "credential": "ctk_secret",
                },
            )
            if observed
            else create_agent(model, [effect], openeight=config())
        )
        assert not client.registrations and not client.requests
        async with context as agent:
            await agent.aflush()
            assert agent.catalog_snapshot().publication_complete
            assert not client.writes and not effects
            result = await agent.ainvoke({"messages": [("user", "go")]})
            assert result["messages"][-1].content == "done"
            assert effects == ["ok"]
            assert bool(client.requests) is not observed
        assert agent.delivery_status().closed

    asyncio.run(run())


def test_mutated_duplicate_rejected_at_entry(monkeypatch):
    @tool
    def one(value: str) -> str:
        """First tool."""
        return value

    second = one.model_copy(update={"name": "two"})
    context = create_agent(
        ToolModel(responses=[AIMessage(content="done")]),
        [one, second],
        openeight=config(),
    )
    second.name = one.name

    async def run():
        with pytest.raises(ConfigurationError, match="Tool definition is unsupported"):
            async with context:
                pytest.fail("mutated duplicate was accepted")

    asyncio.run(run())


@pytest.mark.parametrize("package", _qualified.PACKAGES)
def test_unqualified_version_rejected_before_resources(monkeypatch, package):
    installed_version = _qualified.version
    monkeypatch.setattr(
        _qualified,
        "version",
        lambda name: "99.0.0" if name == package else installed_version(name),
    )
    monkeypatch.setattr(
        _owner, "Client", lambda *args, **kwargs: pytest.fail("client constructed")
    )
    with pytest.raises(ConfigurationError, match="Framework version is unsupported"):
        create_agent(
            ToolModel(responses=[AIMessage(content="done")]), openeight=config()
        )


@pytest.mark.parametrize("mode", ["none", "inputs", "outputs", "all"])
@pytest.mark.parametrize("observed", [False, True])
def test_public_capture_modes_preserve_native_and_governance_inputs(
    monkeypatch, mode, observed
):
    import json

    client = GovernedClient()

    def client_factory(*args, **kwargs):
        assert kwargs["integration"] == "langchain"
        return client

    monkeypatch.setattr(_owner, "Client", client_factory)
    effects, calls = [], []
    marker = "CONTENT-SECRET"

    @tool
    async def echo(value: str) -> str:
        """Echo the native input."""
        effects.append(value)
        return value

    def redact(ctx, value):
        calls.append((ctx.kind, ctx.direction))
        return "[REDACTED]"

    model = ToolModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[dict(name="echo", args={"value": marker}, id="call")],
            ),
            AIMessage(content=marker),
        ]
    )

    async def run():
        cfg = dict(
            agent_name="test",
            endpoint="http://localhost:7777",
            credential="ctk_secret",
            capture_content=mode,
            redact_content=redact,
        )
        manager = (
            observe(model, [echo], openeight={**cfg})
            if observed
            else create_agent(model, [echo], openeight=OpenEightConfig(**cfg))
        )
        async with manager as agent:
            result = await agent.ainvoke({"messages": [("user", marker)]})
            assert result["messages"][-1].content == marker
            await agent.aflush()
            snapshot = agent.delivery_status()
        assert not any(snapshot.content_capture_failures.values())

    asyncio.run(run())
    assert effects == [marker]
    all_events = [e for body in client.writes for e in body.get("events", [])]
    for event in all_events:
        if event.get("decision_id"):
            assert json.loads(event["input_json"])["selected_values"] == [
                {"path": "value", "value": marker}
            ]
        else:
            assert marker not in json.dumps(event)
        if event["observation_kind"] in ("llm_call", "tool_call"):
            if not event.get("decision_id"):
                assert ("input_json" in event) == (mode in ("inputs", "all"))
            assert ("output_json" in event) == (mode in ("outputs", "all"))
        else:
            assert "input_json" not in event and "output_json" not in event
    expected_inputs = 2 + int(observed)
    assert sum(direction == "input" for _, direction in calls) == (
        expected_inputs if mode in ("inputs", "all") else 0
    )
    assert sum(direction == "output" for _, direction in calls) == (
        3 if mode in ("outputs", "all") else 0
    )


@pytest.mark.parametrize("mode", ["none", "inputs", "outputs", "all"])
def test_filtered_content_reaches_fake_http_and_retries_without_recapture(
    monkeypatch, mode
):
    import json

    import httpx

    from openeight import Client, _adapter_http
    from openeight.langchain import _owner

    writes, calls = [], []
    failed = False

    class FakeHTTP:
        def send(self, request, deadline):
            nonlocal failed
            body = json.loads(request.content)
            assert request.url.path.endswith("/WriteTelemetry")
            writes.append(body)
            if body.get("events") and not failed:
                failed = True
                raise httpx.ReadError("lost acknowledgement", request=request)
            return httpx.Response(
                200,
                request=request,
                json={
                    field: [
                        {"id": item["id"], "replayed": False}
                        for item in body.get(field, [])
                    ]
                    for field in ("runs", "events")
                },
            )

        def close(self):
            pass

    monkeypatch.setattr(_owner, "Client", Client)
    monkeypatch.setattr(_adapter_http, "DeadlineHTTPTransport", FakeHTTP)

    def redact(ctx, value):
        calls.append(ctx.direction)
        return "[REDACTED]"

    async def run():
        async with observe(
            ToolModel(responses=[AIMessage(content="HTTP-SECRET")]),
            openeight={
                "agent_name": "test",
                "endpoint": "https://control.test",
                "credential": "ctk_test",
                "capture_content": mode,
                "redact_content": redact,
            },
        ) as agent:
            result = await agent.ainvoke({"messages": [("user", "HTTP-SECRET")]})
            assert result["messages"][-1].content == "HTTP-SECRET"
            await agent.aflush(3)

    asyncio.run(run())
    assert failed and "HTTP-SECRET" not in json.dumps(writes)
    models = [
        e
        for body in writes
        for e in body.get("events", [])
        if e["observation_kind"] == "llm_call"
    ]
    assert models and all(e == models[0] for e in models)
    assert ("input_json" in models[0]) == (mode in ("inputs", "all"))
    assert ("output_json" in models[0]) == (mode in ("outputs", "all"))
    assert calls == (
        ["input"]
        if mode == "inputs"
        else ["output"]
        if mode == "outputs"
        else ["input", "output"]
        if mode == "all"
        else []
    )


@pytest.mark.parametrize("adapter", ["langchain", "deepagents"])
def test_observe_consumes_content_keywords_before_native_builder(monkeypatch, adapter):
    if adapter == "deepagents":
        pytest.importorskip("deepagents")
    import importlib

    builder = importlib.import_module(f"openeight.{adapter}._builder")
    captured = {}

    def context(*args, **kwargs):
        captured.update(kwargs)
        return captured

    monkeypatch.setattr(builder, "_context", context)

    def redact(ctx, value):
        return value

    builder.observe(
        object(),
        openeight={
            "agent_name": "test",
            "endpoint": "http://localhost:7777",
            "credential": "ctk_test",
            "capture_content": "outputs",
            "redact_content": redact,
        },
        system_prompt="native prompt",
    )
    governance = captured.pop("config").governance
    assert (
        governance.capture_content == "outputs" and governance.redact_content is redact
    )
    assert "capture_content" not in captured and "redact_content" not in captured
    assert captured["system_prompt"] == "native prompt"


def test_empty_tool_observation_first_real_request_declares_langchain(monkeypatch):
    import json

    import httpx

    requests = []

    def respond(request):
        requests.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                field: [
                    {"id": item["id"], "replayed": False}
                    for item in body.get(field, [])
                ]
                for field in ("runs", "events")
            },
        )

    from openeight._adapter_http import DeadlineHTTPTransport

    monkeypatch.setattr(
        DeadlineHTTPTransport, "send", lambda self, request, deadline: respond(request)
    )

    async def run():
        async with observe(
            ToolModel(responses=[AIMessage(content="done")]),
            [],
            openeight={
                "agent_name": "empty",
                "endpoint": "http://localhost:7777",
                "credential": "ctk_secret",
            },
        ) as agent:
            await agent.ainvoke({"messages": [("user", "go")]})
            await agent.aflush()

    asyncio.run(run())
    assert requests
    assert all(
        request.headers["X-OpenEight-Agent-Integration"] == "langchain"
        for request in requests
    )
    assert requests[0].url.path.endswith("WriteTelemetry")
