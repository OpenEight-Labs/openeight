import json

import httpx
import pytest

from openeight import Client, ToolRegistration


def test_register_tools_uses_observe_connect_path():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "request_index": 0,
                        "status": "invalid",
                        "reason": "invalid_registration",
                    }
                ]
            },
        )

    client = Client(
        "https://example.test",
        api_key="ctk_secret",
        agent_name="agent",
        transport=httpx.MockTransport(handler),
    )
    result = client.register_tools(
        [ToolRegistration(name="x", description="x", input_schema={})]
    )
    assert result[0].status == "invalid"
    assert (
        requests[0].url.path
        == "/api/v1/openeight.observe.v1.ObserveService/RegisterTools"
    )
    assert json.loads(requests[0].content)["tools"][0]["input_schema_json"] == "{}"
    assert "field_selection" not in json.loads(requests[0].content)["tools"][0]
    client.close()


def test_registration_wire_contains_only_facts(monkeypatch):
    monkeypatch.setattr("openeight._sdk_version.version", lambda _: "0.0.1")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "request_index": 0,
                        "status": "invalid",
                        "reason": "invalid_registration",
                    }
                ]
            },
        )

    with Client(
        "https://example.test",
        api_key="ctk_secret",
        agent_name="agent",
        transport=httpx.MockTransport(handler),
    ) as client:
        client.register_tools(
            [ToolRegistration(name="x", description="x", input_schema={})]
        )
    assert json.loads(requests[0].content) == {
        "tools": [{"name": "x", "description": "x", "input_schema_json": "{}"}],
        "sdk_version": __import__("openeight").__version__
    }


def test_tool_version_decodes_nullable_intent_and_freezes_status():
    import pytest

    from openeight.tools import _parse_tool_version

    wire = {
        "id": "00000000-0000-4000-8000-000000000001",
        "tool_id": "00000000-0000-4000-8000-000000000002",
        "version": "1",
        "content_digest": "sha256:" + "a" * 64,
        "input_schema_json": "{}",
        "created_at": "2026-10-12T00:00:00Z",
    }
    assert _parse_tool_version(wire).intent is None
    assert _parse_tool_version({**wire, "intent": None}).intent_drafting is None
    value = "  Require approval before every call.\n"
    parsed = _parse_tool_version(
        {**wire, "intent": value, "intent_drafting": {"status": "complete"}}
    )
    assert parsed.intent == value
    assert parsed.intent_drafting["status"] == "complete"
    with pytest.raises(TypeError):
        parsed.intent_drafting["status"] = "running"


@pytest.mark.parametrize(
    "capability,governed,reason,eligible,ready,accepted",
    [
        ("read", False, "read_only", True, True, True),
        ("write", True, "ready", True, True, False),
        ("write", False, "read_only", True, True, False),
        ("", False, "read_only", True, True, False),
        ("read", False, "ready", True, True, False),
        ("read", False, "read_only", True, False, False),
        ("read", False, "read_only", False, True, False),
    ],
)
def test_ready_registration_without_digest_requires_read_only(
    capability, governed, reason, eligible, ready, accepted
):
    from openeight.errors import ProtocolError

    tool_id = "00000000-0000-4000-8000-000000000001"
    version_id = "00000000-0000-4000-8000-000000000002"
    row = {
        "request_index": 0,
        "status": "replayed",
        "reason": "",
        "tool": {
            "id": tool_id,
            "name": "x",
            "description": "x",
            "capability": capability,
            "governed": governed,
            "created_at": "2026-10-14T00:00:00Z",
        },
        "tool_version": {
            "id": version_id,
            "tool_id": tool_id,
            "version": "schema:1",
            "content_digest": "sha256:" + "0" * 64,
            "input_schema_json": "{}",
            "created_at": "2026-10-14T00:00:00Z",
        },
        "readiness": {
            "request_index": 0,
            "name": "x",
            "tool_id": tool_id,
            "tool_version_id": version_id,
            "registration_status": "registered",
            "governed": governed,
            "policy_ready": ready,
            "eligible": eligible,
            "reason": reason,
        },
    }
    with Client(
        "https://example.test",
        api_key="ctk_secret",
        agent_name="agent",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"results": [row]})
        ),
    ) as client:
        facts = [ToolRegistration(name="x", description="x", input_schema={})]
        if accepted:
            assert client.register_tools(facts)[0].readiness.reason == "read_only"
        else:
            with pytest.raises(ProtocolError):
                client.register_tools(facts)
