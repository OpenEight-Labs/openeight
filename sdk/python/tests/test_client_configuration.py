import inspect
import math

import httpx
import inspect
import pytest

from openeight import Client, ConfigurationError


def test_buffered_observe_has_the_exact_explicit_keyword_only_signature():
    assert str(inspect.signature(Client.buffered_observe)) == (
        "(self, *, max_records: 'int' = 1000, max_bytes: 'int' = 8388608, "
        "flush_records: 'int' = 100, flush_bytes: 'int' = 3145728, "
        "flush_interval: 'float' = 1.0, max_request_records: 'int' = 1000, "
        "max_request_bytes: 'int' = 4194304) -> 'BufferedObserveWriter'"
    )


def _assert_configuration_error(call, reason: str, message: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        call()
    assert caught.value.reason == reason
    assert caught.value.message == message
    assert str(caught.value) == message
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


class _ExplodingString(str):
    def __str__(self):
        raise RuntimeError("validator-internal-canary")


class _ExplodingBooleanString(str):
    def __bool__(self):
        raise RuntimeError("validator-boolean-canary")


def _decide_with_value(field, value):
    kwargs = {
        "decision_id": "",
        "tool_id": "00000000-0000-4000-8000-000000000001",
        "tool_version_id": "00000000-0000-4000-8000-000000000002",
        field: value,
    }
    kwargs.update(input={}, policy_contract_set_digest="a" * 64, selected_values=[])
    if field == "decision_id":
        kwargs["source_time"] = "2026-08-31T12:00:00Z"
    elif field == "source_time":
        kwargs["decision_id"] = "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b11"
    with Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
    ) as client:
        client.decide(**kwargs)


def test_client_constructor_and_from_env_signatures_are_exact():
    signature = inspect.signature(Client)
    assert list(signature.parameters) == [
        "base_url",
        "api_key",
        "agent_name",
        "integration",
        "timeout",
        "transport",
    ]
    assert (
        signature.parameters["base_url"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    )
    assert signature.parameters["base_url"].default is inspect.Parameter.empty
    for name in ("api_key", "agent_name", "integration", "timeout", "transport"):
        assert signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["api_key"].default is inspect.Parameter.empty
    assert signature.parameters["agent_name"].default is inspect.Parameter.empty
    assert signature.parameters["timeout"].default == 30.0
    assert signature.parameters["transport"].default is None

    from_env = inspect.signature(Client.from_env)
    assert list(from_env.parameters) == ["timeout", "transport"]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in from_env.parameters.values()
    )
    assert from_env.parameters["timeout"].default == 30.0
    assert from_env.parameters["transport"].default is None


@pytest.mark.parametrize(
    "call",
    [
        lambda: Client(),
        lambda: Client("https://control.example"),
        lambda: Client("https://control.example", api_key="ctk_secret"),
        lambda: Client(
            "https://control.example",
            agent_name="runtime/worker",
        ),
        lambda: Client(
            "https://control.example",
            "ctk_secret",
            "runtime/worker",
        ),
    ],
)
def test_omitted_or_positional_constructor_requirements_use_python_binding(call):
    with pytest.raises(TypeError):
        call()


@pytest.mark.parametrize(
    ("base_url", "reason", "message"),
    [
        (None, "missing_base_url", "base_url is required"),
        ("", "missing_base_url", "base_url is required"),
        ("   ", "missing_base_url", "base_url is required"),
        ("relative/path", "invalid_base_url", "base_url is invalid"),
        ("ftp://control.example", "invalid_base_url", "base_url is invalid"),
        (
            "https://user:secret@control.example",
            "invalid_base_url",
            "base_url is invalid",
        ),
        (
            "https://control.example?debug=true",
            "invalid_base_url",
            "base_url is invalid",
        ),
        ("https://control.example#fragment", "invalid_base_url", "base_url is invalid"),
        ("https:///missing-host", "invalid_base_url", "base_url is invalid"),
        ("https://bad host", "invalid_base_url", "base_url is invalid"),
        ("https://999.999.999.999", "invalid_base_url", "base_url is invalid"),
        ("https://control.example/\ud800", "invalid_base_url", "base_url is invalid"),
    ],
)
def test_client_rejects_missing_or_malformed_base_url(base_url, reason, message):
    _assert_configuration_error(
        lambda: Client(
            base_url,
            api_key="ctk_secret",
            agent_name="runtime/worker",
        ),
        reason,
        message,
    )


@pytest.mark.parametrize(
    ("api_key", "reason", "message"),
    [
        (None, "missing_api_key", "api_key is required"),
        ("", "missing_api_key", "api_key is required"),
        ("   ", "missing_api_key", "api_key is required"),
        ("ctp_operator", "invalid_api_key", "api_key must use ctk_ prefix"),
        (" ctk_secret", "invalid_api_key", "api_key must use ctk_ prefix"),
    ],
)
def test_client_rejects_missing_or_non_machine_api_key(api_key, reason, message):
    _assert_configuration_error(
        lambda: Client(
            "https://control.example",
            api_key=api_key,
            agent_name="runtime/worker",
        ),
        reason,
        message,
    )


@pytest.mark.parametrize(
    ("agent_name", "reason", "message"),
    [
        (None, "missing_agent_name", "agent_name is required"),
        ("", "missing_agent_name", "agent_name is required"),
        ("   ", "missing_agent_name", "agent_name is required"),
        ("Runtime/Worker", "invalid_agent_name", "agent_name is invalid"),
        ("-runtime", "invalid_agent_name", "agent_name is invalid"),
        ("runtime worker", "invalid_agent_name", "agent_name is invalid"),
        ("a" * 129, "invalid_agent_name", "agent_name is invalid"),
    ],
)
def test_client_enforces_server_agent_name_syntax(agent_name, reason, message):
    _assert_configuration_error(
        lambda: Client(
            "https://control.example",
            api_key="ctk_secret",
            agent_name=agent_name,
        ),
        reason,
        message,
    )


@pytest.mark.parametrize("agent_name", ["a", "a._/-0", "a" * 128])
def test_client_accepts_the_complete_server_agent_name_boundary(agent_name):
    client = Client(
        "https://control.example",
        api_key="ctk_",
        agent_name=agent_name,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
    )
    client.close()


@pytest.mark.parametrize(
    "timeout",
    [
        0,
        -1,
        math.inf,
        -math.inf,
        math.nan,
        True,
        "30",
        pytest.param(10**10_000, id="huge-int"),
    ],
)
def test_client_requires_finite_positive_timeout(timeout):
    _assert_configuration_error(
        lambda: Client(
            "https://control.example",
            api_key="ctk_secret",
            agent_name="runtime/worker",
            timeout=timeout,
        ),
        "invalid_timeout",
        "timeout must be finite and greater than zero",
    )


@pytest.mark.parametrize(
    ("call", "reason", "message"),
    [
        (
            lambda: Client(
                "https://control.example:invalid-port",
                api_key="ctk_secret",
                agent_name="runtime/worker",
            ),
            "invalid_base_url",
            "base_url is invalid",
        ),
        (
            lambda: Client(
                "https://control.example",
                api_key="ctk_secret",
                agent_name="runtime/worker",
                transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
            ).decide(
                decision_id="invalid-decision-canary",
                source_time="2026-08-31T12:00:00Z",
                tool_id="00000000-0000-4000-8000-000000000001",
                tool_version_id="00000000-0000-4000-8000-000000000002",
                input={},
                policy_contract_set_digest="a" * 64,
                selected_values=[],
            ),
            "invalid_decision_id",
            "decision_id must be a UUIDv7",
        ),
        (
            lambda: Client(
                "https://control.example",
                api_key="ctk_secret",
                agent_name="runtime/worker",
                transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
            ).decide(
                decision_id="018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b11",
                source_time="invalid-source-time-canary",
                tool_id="00000000-0000-4000-8000-000000000001",
                tool_version_id="00000000-0000-4000-8000-000000000002",
                input={},
                policy_contract_set_digest="a" * 64,
                selected_values=[],
            ),
            "invalid_source_time",
            "source_time must be timezone-aware RFC3339",
        ),
        (
            lambda: Client(
                "https://control.example",
                api_key="ctk_secret",
                agent_name="runtime/worker",
                transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
            ).decide(
                tool_id="00000000-0000-4000-8000-000000000001",
                tool_version_id="00000000-0000-4000-8000-000000000002",
                input={},
                selected_values=[{"path": "not_json", "value": object()}],
                policy_contract_set_digest="a" * 64,
            ),
            "invalid_input",
            "input must be a strict JSON object",
        ),
    ],
)
def test_local_validation_errors_drop_parser_and_encoder_context(call, reason, message):
    _assert_configuration_error(call, reason, message)


@pytest.mark.parametrize(
    ("call", "reason", "message"),
    [
        (
            lambda: Client(
                _ExplodingString("https://control.example"),
                api_key="ctk_secret",
                agent_name="runtime/worker",
            ),
            "invalid_base_url",
            "base_url is invalid",
        ),
        (
            lambda: Client(
                "https://control.example",
                api_key=_ExplodingString("ctk_secret"),
                agent_name="runtime/worker",
            ),
            "invalid_api_key",
            "api_key must use ctk_ prefix",
        ),
        (
            lambda: Client(
                "https://control.example",
                api_key="ctk_secret",
                agent_name=_ExplodingString("runtime/worker"),
            ),
            "invalid_agent_name",
            "agent_name is invalid",
        ),
        (
            lambda: _decide_with_value(
                "decision_id", _ExplodingBooleanString("invalid")
            ),
            "invalid_decision_id",
            "decision_id must be a UUIDv7",
        ),
        (
            lambda: _decide_with_value("tool_id", _ExplodingString("refund.create")),
            "invalid_tool_id",
            "tool_id must be a UUID",
        ),
        (
            lambda: _decide_with_value(
                "source_time", _ExplodingString("2026-08-31T12:00:00Z")
            ),
            "invalid_source_time",
            "source_time must be timezone-aware RFC3339",
        ),
    ],
)
def test_validator_exception_boundaries_are_total(call, reason, message):
    _assert_configuration_error(call, reason, message)


def test_blank_explicit_source_time_is_not_replaced_with_current_time():
    _assert_configuration_error(
        lambda: Client(
            "https://control.example",
            api_key="ctk_secret",
            agent_name="runtime/worker",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
        ).decide(
            decision_id="018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b11",
            source_time="",
            tool_id="00000000-0000-4000-8000-000000000001",
            tool_version_id="00000000-0000-4000-8000-000000000002",
            input={},
            policy_contract_set_digest="a" * 64,
            selected_values=[],
        ),
        "invalid_source_time",
        "source_time must be timezone-aware RFC3339",
    )


@pytest.mark.parametrize(
    "source_time",
    [
        "2026-08-31 12:00:00Z",
        "2026-08-31T12:00:00+0000",
        "2026-08-31T12:00:00z",
        "2026-08-31T12:00:00",
        "2026-08-31",
    ],
)
def test_source_time_requires_strict_rfc3339_syntax(source_time):
    _assert_configuration_error(
        lambda: Client(
            "https://control.example",
            api_key="ctk_secret",
            agent_name="runtime/worker",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
        ).decide(
            decision_id="018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b11",
            source_time=source_time,
            tool_id="00000000-0000-4000-8000-000000000001",
            tool_version_id="00000000-0000-4000-8000-000000000002",
            input={},
            policy_contract_set_digest="a" * 64,
            selected_values=[],
        ),
        "invalid_source_time",
        "source_time must be timezone-aware RFC3339",
    )


def test_explicit_none_run_id_is_not_treated_as_omitted():
    _assert_configuration_error(
        lambda: Client(
            "https://control.example",
            api_key="ctk_secret",
            agent_name="runtime/worker",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
        ).decide(
            run_id=None,
            tool_id="00000000-0000-4000-8000-000000000001",
            tool_version_id="00000000-0000-4000-8000-000000000002",
            input={},
            policy_contract_set_digest="a" * 64,
            selected_values=[],
        ),
        "invalid_run_id",
        "run_id must be a UUIDv7",
    )


def test_direct_construction_ignores_all_environment_configuration(monkeypatch):
    monkeypatch.setenv("OPENEIGHT_ENDPOINT", "https://wrong.example")
    monkeypatch.setenv("OPENEIGHT_API_KEY", "ctk_wrong")
    monkeypatch.setenv("OPENEIGHT_AGENT_NAME", "wrong-agent")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok"})

    with Client(
        "HTTPS://control.example///",
        api_key="ctk_right",
        agent_name="runtime/worker",
        transport=httpx.MockTransport(handler),
    ) as client:
        assert client.health() == {"status": "ok"}

    assert str(requests[0].url) == "https://control.example/healthz"
    assert requests[0].headers.get("Authorization") is None
    assert requests[0].headers.get("X-OpenEight-Agent-Name") is None


def test_from_env_requires_only_the_three_machine_settings(monkeypatch):
    for name in (
        "OPENEIGHT_ENDPOINT",
        "OPENEIGHT_API_KEY",
        "OPENEIGHT_AGENT_NAME",
    ):
        monkeypatch.delenv(name, raising=False)

    _assert_configuration_error(
        Client.from_env,
        "missing_base_url",
        "base_url is required",
    )
    monkeypatch.setenv("OPENEIGHT_ENDPOINT", "https://control.example")
    _assert_configuration_error(
        Client.from_env,
        "missing_api_key",
        "api_key is required",
    )
    monkeypatch.setenv("OPENEIGHT_API_KEY", "ctk_secret")
    _assert_configuration_error(
        Client.from_env,
        "missing_agent_name",
        "agent_name is required",
    )


@pytest.mark.parametrize(
    ("name", "reason", "message"),
    [
        ("OPENEIGHT_ENDPOINT", "missing_base_url", "base_url is required"),
        ("OPENEIGHT_API_KEY", "missing_api_key", "api_key is required"),
        ("OPENEIGHT_AGENT_NAME", "missing_agent_name", "agent_name is required"),
    ],
)
def test_from_env_rejects_blank_required_values(monkeypatch, name, reason, message):
    monkeypatch.setenv("OPENEIGHT_ENDPOINT", "https://control.example")
    monkeypatch.setenv("OPENEIGHT_API_KEY", "ctk_secret")
    monkeypatch.setenv("OPENEIGHT_AGENT_NAME", "runtime/worker")
    monkeypatch.setenv(name, "   ")

    _assert_configuration_error(Client.from_env, reason, message)


def test_from_env_builds_valid_client(monkeypatch):
    requests = []
    monkeypatch.setenv("OPENEIGHT_ENDPOINT", "https://control.example/")
    monkeypatch.setenv("OPENEIGHT_API_KEY", "ctk_secret")
    monkeypatch.setenv("OPENEIGHT_AGENT_NAME", "runtime/worker")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok"})

    with Client.from_env(transport=httpx.MockTransport(handler)) as client:
        client.health()

    assert str(requests[0].url) == "https://control.example/healthz"


def test_client_exposes_no_id_bearing_admin_plane():
    for method in (
        "ready",
        "provision_agent",
        "issue_credential",
        "task",
        "submit_hitl",
        "list_audit_entries",
        "list_audit_entries_page",
        "get_costs",
        "export_bundle",
    ):
        assert not hasattr(Client, method), method
