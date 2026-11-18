"""Explicit provider configuration and native OpenAI tool-call translation."""

from dataclasses import fields, replace

import pytest

from contracts import Settings
from models import build_models


def settings(provider):
    return Settings(
        "http://localhost:7777",
        "incident-provider-test",
        "ctk_test",
        mode="provider",
        provider_model=provider,
        provider_credential="explicit-test-key",
    )


@pytest.mark.parametrize(
    "provider,module,constructor",
    [
        ("anthropic:chosen-model", "langchain_anthropic", "ChatAnthropic"),
        ("openai:chosen-model", "langchain_openai", "ChatOpenAI"),
    ],
)
def test_every_role_uses_explicit_provider_model_and_key(
    monkeypatch, provider, module, constructor
):
    adapter = pytest.importorskip(module)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unexpected.invalid/v1")
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(adapter, constructor, create)
    models = build_models(settings=settings(provider), scenario="rollback")
    assert (
        len(
            {
                id(getattr(models, field.name))
                for field in fields(models)
                if getattr(models, field.name) is not None
            }
        )
        == 4
    )
    expected = {
        "model": "chosen-model",
        "api_key": "explicit-test-key",
        "max_retries": 0,
    }
    if provider.startswith("openai:"):
        expected["base_url"] = "https://api.openai.com/v1"
    assert calls == [expected] * 4


@pytest.mark.parametrize(
    "model", ["openai:", "openai: ", "chosen-model", "unknown:chosen-model"]
)
def test_invalid_provider_or_model_cannot_fall_back(model):
    with pytest.raises(ValueError, match="unsupported_provider"):
        build_models(settings=settings(model), scenario="investigate")


def test_provider_never_uses_ambient_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key")
    with pytest.raises(ValueError, match="provider_configuration_required"):
        build_models(
            settings=replace(settings("openai:chosen-model"), provider_credential=None),
            scenario="investigate",
        )


async def test_openai_adapter_binds_native_tools_and_returns_usage():
    pytest.importorskip("langchain_openai")
    import httpx
    from langchain_core.messages import HumanMessage
    from runner import new_simulator
    from tools import build_tools

    requests = []

    def respond(request):
        import json

        body = json.loads(request.content)
        requests.append(body)
        assert request.headers["authorization"] == "Bearer explicit-test-key"
        assert body["model"] == "chosen-model"
        assert any(t["function"]["name"] == "read_logs" for t in body["tools"])
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": "chosen-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "lookup-call",
                                    "type": "function",
                                    "function": {
                                        "name": "read_logs",
                                        "arguments": '{"service_id":"checkout"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 4,
                    "total_tokens": 16,
                },
            },
        )

    transport = httpx.MockTransport(respond)
    async with httpx.AsyncClient(transport=transport) as client:
        from langchain_openai import ChatOpenAI

        model = ChatOpenAI(
            model="chosen-model",
            api_key="explicit-test-key",
            max_retries=0,
            http_async_client=client,
        )
        reply = await model.bind_tools(
            build_tools(simulator=new_simulator(), role="diagnostics")
        ).ainvoke([HumanMessage(content="Investigate checkout")])
    assert len(requests) == 1
    assert reply.tool_calls[0]["name"] == "read_logs"
    assert reply.tool_calls[0]["args"] == {"service_id": "checkout"}
    assert reply.usage_metadata["input_tokens"] == 12
    assert reply.usage_metadata["output_tokens"] == 4
