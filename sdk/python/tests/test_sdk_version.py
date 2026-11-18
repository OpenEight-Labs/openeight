import json
import tomllib
from pathlib import Path

import httpx
import pytest

import openeight
from openeight import Client, ToolRegistration, _sdk_version


def test_fallback_matches_package_version(monkeypatch):
    def missing(_name):
        raise _sdk_version.PackageNotFoundError

    monkeypatch.setattr(_sdk_version, "version", missing)
    assert _sdk_version.resolve_sdk_version() == openeight.__version__
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert project["project"]["version"] == openeight.__version__


@pytest.mark.parametrize("label", ["", "v1", "1\n", "1é", "1/2", "1" * 129, None])
def test_invalid_metadata_is_omitted(monkeypatch, label):
    monkeypatch.setattr(_sdk_version, "version", lambda _: label)
    assert _sdk_version.resolve_sdk_version() is None


def test_client_resolves_once_and_reports_on_repeated_registration(monkeypatch):
    monkeypatch.setattr(_sdk_version, "version", lambda _: "1.2.dev3+local")
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
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
        monkeypatch.setattr(_sdk_version, "version", lambda _: "9.0")
        for _ in range(2):
            client.register_tools(
                [ToolRegistration(name="lookup", description="", input_schema={})]
            )
    assert requests[0] == requests[1]
    assert requests[0]["sdk_version"] == "1.2.dev3+local"


def test_missing_version_keeps_registration_available(monkeypatch):
    monkeypatch.setattr(_sdk_version, "version", lambda _: "invalid")

    def handler(request):
        assert "sdk_version" not in json.loads(request.content)
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
            [ToolRegistration(name="lookup", description="", input_schema={})]
        )
