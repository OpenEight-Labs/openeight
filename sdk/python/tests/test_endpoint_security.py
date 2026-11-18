"""Control credentials are sent only to explicit secure or loopback endpoints."""

import asyncio

import httpx
import pytest

from openeight import Client, ConfigurationError, Governance, ProtocolError, SDKError
from openeight._adapter_http import DeadlineHTTPTransport


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://control.example",
        "http://10.0.0.1",
        "http://192.168.1.1",
        "http://0.0.0.0",
        "http://[::]",
        "http://[2001:db8::1]",
        "http://localhost.example",
        "http://localhost.",
        "http://localhost@control.example",
        "http://control.example@localhost",
        "http://127.0.0.1.example",
        "http://2130706433",
        "http://127.1",
        "http://0177.0.0.1",
        "http://0x7f000001",
        "http://[::ffff:127.0.0.1]",
        "http://[::1%25eth0]",
        "http://%6cocalhost",
        "http://localhost\\@control.example",
    ],
)
def test_insecure_endpoint_rejected_before_transport_or_credentials(
    monkeypatch, endpoint
):
    def forbidden(*args, **kwargs):
        pytest.fail("rejected endpoint constructed a transport")

    monkeypatch.setattr(httpx, "Client", forbidden)
    values = dict(agent_name="test", credential="ctk_do_not_send", endpoint=endpoint)
    with pytest.raises(ConfigurationError) as caught:
        Client(endpoint, api_key=values["credential"], agent_name="test")
    assert caught.value.reason == "invalid_base_url"
    assert "ctk_do_not_send" not in str(caught.value)
    with pytest.raises(ConfigurationError, match="base_url is invalid"):
        Governance(**values)
    monkeypatch.setenv("OPENEIGHT_ENDPOINT", endpoint)
    monkeypatch.setenv("OPENEIGHT_API_KEY", values["credential"])
    monkeypatch.setenv("OPENEIGHT_AGENT_NAME", "test")
    with pytest.raises(ConfigurationError, match="base_url is invalid"):
        Client.from_env()


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://control.example/prefix/",
        "https://10.0.0.1",
        "http://localhost:7777",
        "HTTP://LOCALHOST:7777",
        "http://127.0.0.1",
        "http://127.255.255.254:7777",
        "http://[::1]:7777",
    ],
)
def test_explicit_secure_and_loopback_endpoints_preserve_authentication(endpoint):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "ok"})

    with Client(
        endpoint,
        api_key="ctk_test",
        agent_name="test",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ProtocolError):
            client.get_tool_readiness([])
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == "ApiKey ctk_test"


def test_redirect_cannot_forward_credentials_to_insecure_endpoint():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "http://attacker.example"})

    with Client(
        "https://control.example",
        api_key="ctk_test",
        agent_name="test",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(SDKError):
            client.get_tool_readiness([])
    assert len(requests) == 1
    assert requests[0].url.host == "control.example"
    assert requests[0].headers["Authorization"] == "ApiKey ctk_test"


def test_default_transports_ignore_ambient_proxy_and_ca_overrides(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("SSL_CERT_FILE", "/does-not-exist/ca.pem")
    with Client("http://localhost:7777", api_key="ctk_test", agent_name="test"):
        pass

    transport = DeadlineHTTPTransport()

    async def initialize():
        # Exercise the actual owned async-client construction without network I/O.
        original = httpx.AsyncClient

        def client(**kwargs):
            return original(
                **kwargs,
                transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            )

        monkeypatch.setattr(httpx, "AsyncClient", client)
        request = httpx.Request("GET", "http://localhost:7777/healthz")
        import time

        return await transport._send_until_deadline(request, time.monotonic() + 5)

    try:
        response = asyncio.run_coroutine_threadsafe(
            initialize(), transport.loop
        ).result(5)
        assert response.status_code == 200
    finally:
        transport.close()


@pytest.mark.parametrize("adapter", ["langchain", "deepagents"])
def test_observation_factories_reject_insecure_control_endpoints(adapter):
    import importlib

    pytest.importorskip(adapter)
    module = importlib.import_module(f"openeight.{adapter}")
    with pytest.raises(ConfigurationError, match="base_url is invalid"):
        module.observe(
            object(),
            openeight={
                "agent_name": "test",
                "credential": "ctk_test",
                "endpoint": "http://remote.example",
            },
        )
