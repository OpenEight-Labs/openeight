import asyncio
from importlib.util import find_spec

import pytest
from control_service import ControlService
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from openeight import APIError, BlockedError, OpenEightConfig
from openeight.langchain import ApprovalConfig

from tests.support.http_control import ControlHTTP

if find_spec("deepagents") is not None:
    import deepagents_agent
else:
    deepagents_agent = None

import langchain_agent

from refund_assistant.cli import blocked, configuration, run_async, run_sync
from refund_assistant.business import RefundRequest

FRAMEWORKS = [langchain_agent] + ([deepagents_agent] if deepagents_agent else [])


class ToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture(autouse=True)
def control(monkeypatch):
    ControlService.instances = []
    ControlService.outcome = "allow"
    ControlService.pending_check = None
    with ControlHTTP(ControlService):
        yield


def config():
    return OpenEightConfig(
        endpoint="http://localhost:7777",
        credential="ctk_test",
        agent_name="refund",
        on_block="raise",
        approval=ApprovalConfig(
            timeout=0.04, poll_interval=0.001, poll_max_interval=0.002
        ),
    )


def model(scenario, missing=False):
    def call(name, args, identity):
        return AIMessage(
            content="", tool_calls=[{"name": name, "args": args, "id": identity}]
        )

    return ToolModel(
        responses=(
            [
                call("lookup_order", {"order_id": scenario.order.order_id}, "lookup"),
                call(
                    "issue_refund",
                    {
                        "order_id": scenario.order.order_id,
                        "amount": scenario.json_requested_amount,
                        "currency": scenario.order.currency,
                    },
                    "refund",
                ),
            ]
            if not missing
            else []
        )
        + [AIMessage(content="done")]
    )


def execute(framework, mode, scenario, command="run", missing=False):
    owner = framework.build(model(scenario, missing), scenario, config())
    value = (
        run_sync(owner, scenario, command)
        if mode == "sync"
        else asyncio.run(run_async(owner, scenario, command))
    )
    return value, ControlService.instances[-1]


@pytest.mark.parametrize("framework", FRAMEWORKS)
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize(
    "outcome,order_id,state",
    [
        ("allow", "ORD-1041", "receipt_recorded"),
        ("approved", "ORD-1042", "receipt_recorded"),
        ("deny", "ORD-1043", "deny"),
        ("rejected", "ORD-1042", "approval_rejected"),
        ("expired", "ORD-1042", "approval_expired"),
        ("pending", "ORD-1042", "approval_wait_timed_out"),
        ("stale", "ORD-1042", "AUTHORIZATION_STALE"),
    ],
)
def test_native_attempts(framework, mode, outcome, order_id, state):
    ControlService.outcome = outcome
    scenario = RefundRequest(order_id)
    ControlService.pending_check = staticmethod(
        lambda: not scenario.receipts or pytest.fail("effect while pending")
    )
    result, client = execute(framework, mode, scenario)
    assert result["state"] == state
    assert len(scenario.receipts) == (1 if state == "receipt_recorded" else 0)
    assert client.requests
    assert all(
        request["selected_values"]
        == [
            {"path": "amount", "value": scenario.json_requested_amount},
            {"path": "currency", "value": scenario.order.currency},
        ]
        for request in client.requests
    )
    assert len(client.requests) == (2 if outcome == "approved" else 1)
    if outcome == "approved":
        assert client.requests[0] == client.requests[1]
    if outcome == "stale":
        assert "decision_id" not in result and "run_id" not in result
    elif state != "receipt_recorded":
        assert result["decision_id"] == client.requests[0]["decision_id"]
    events = [
        event
        for batch in client.writes
        for event in batch.get("events", [])
        if event.get("observation_kind") == "tool_call"
    ]
    refund_id = client.requests[0]["tool_id"]
    assert any(event.get("tool_id") != refund_id for event in events)
    assert len([event for event in events if event.get("tool_id") == refund_id]) == (
        1 if state == "receipt_recorded" else 0
    )
    # A late server approval cannot restart the completed local invocation.
    if outcome == "pending":
        client.outcome = "approved"
        assert client.approval_state() == "approved" and not scenario.receipts


@pytest.mark.parametrize("framework", FRAMEWORKS)
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_publish_and_missing_attempt(framework, mode):
    scenario = RefundRequest("ORD-1041")
    result, client = execute(framework, mode, scenario, "publish")
    assert result["catalog_delivery"] == "acknowledged" and result["bindings"]
    assert not client.requests and not client.writes and not scenario.receipts
    result, client = execute(framework, mode, scenario, missing=True)
    assert result["state"] == "missing_refund_attempt" and not client.requests


@pytest.mark.parametrize("framework", FRAMEWORKS)
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("outcome", ["unavailable", "revoked"])
def test_service_unavailable_has_no_effect(framework, mode, outcome):
    ControlService.outcome = outcome
    scenario = RefundRequest("ORD-1041")
    with pytest.raises(APIError):
        execute(framework, mode, scenario)
    assert not scenario.receipts


def test_safe_blocked_result_and_one_hour_config(monkeypatch):
    for key, value in {
        "OPENEIGHT_ENDPOINT": "http://localhost:7777",
        "OPENEIGHT_API_KEY": "ctk_test",
        "OPENEIGHT_AGENT_NAME": "refund",
    }.items():
        monkeypatch.setenv(key, value)
    assert configuration().approval.timeout == 3600
    assert configuration().on_block == "raise"
    assert blocked(BlockedError(None, "governance_unconfigured")) == {
        "state": "governance_unconfigured",
        "explanation": "Execution blocked without a returned Decision.",
    }


@pytest.mark.parametrize("command", ["publish", "run"])
def test_cli_output_serializes(monkeypatch, capsys, command):
    import json
    import sys
    from refund_assistant.cli import run_cli

    for key, value in {
        "OPENEIGHT_ENDPOINT": "http://localhost:7777",
        "OPENEIGHT_API_KEY": "ctk_test",
        "OPENEIGHT_AGENT_NAME": "refund",
        "AGENT_MODEL": "openai:explicit-test-model",
        "AGENT_MODEL_API_KEY": "explicit-provider-key",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        "refund_assistant.config.init_chat_model", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(sys, "argv", ["langchain_agent.py", command])
    assert (
        run_cli(
            lambda _, scenario, config: langchain_agent.build(
                model(scenario), scenario, config
            )
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == (
        "publication" if command == "publish" else "receipt_recorded"
    )


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_model_client_uses_only_explicit_provider_key(monkeypatch, provider):
    from unittest.mock import Mock
    from refund_assistant.cli import model_client

    monkeypatch.setenv("AGENT_MODEL", f"{provider}:chosen-model")
    monkeypatch.setenv("AGENT_MODEL_API_KEY", "explicit-provider-key")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key-must-not-be-used")
    monkeypatch.setenv("OPENEIGHT_API_KEY", "ctk_unrelated")
    factory = Mock()
    monkeypatch.setattr("refund_assistant.config.init_chat_model", factory)
    assert model_client() is factory.return_value
    factory.assert_called_once_with(
        "chosen-model", model_provider=provider, api_key="explicit-provider-key"
    )


@pytest.mark.parametrize("key", [None, "", "   "])
def test_model_client_rejects_missing_explicit_key(monkeypatch, key):
    from unittest.mock import Mock
    from refund_assistant.cli import model_client

    monkeypatch.setenv("AGENT_MODEL", "openai:chosen-model")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key-must-not-be-used")
    monkeypatch.delenv("AGENT_MODEL_API_KEY", raising=False)
    if key is not None:
        monkeypatch.setenv("AGENT_MODEL_API_KEY", key)
    factory = Mock()
    monkeypatch.setattr("refund_assistant.config.init_chat_model", factory)
    with pytest.raises(ValueError, match="model_api_key_required"):
        model_client()
    factory.assert_not_called()


@pytest.mark.parametrize(
    "selected", ["chosen-model", "openai:", ":chosen-model", "openai:   "]
)
def test_model_client_requires_explicit_provider_and_model(monkeypatch, selected):
    from refund_assistant.cli import model_client

    monkeypatch.setenv("AGENT_MODEL", selected)
    monkeypatch.setenv("AGENT_MODEL_API_KEY", "explicit-provider-key")
    with pytest.raises(ValueError, match="explicit_model_required"):
        model_client()


def test_native_openai_client_does_not_use_ambient_key(monkeypatch):
    from refund_assistant.cli import model_client

    monkeypatch.setenv("AGENT_MODEL", "openai:explicit-test-model")
    monkeypatch.setenv("AGENT_MODEL_API_KEY", "explicit-provider-key")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key-must-not-be-used")
    client = model_client()
    assert client.openai_api_key.get_secret_value() == "explicit-provider-key"


@pytest.mark.parametrize("framework", FRAMEWORKS)
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize(
    "outcome,state",
    [
        ("allow", "receipt_recorded"),
        ("approved", "receipt_recorded"),
        ("deny", "deny"),
        ("rejected", "approval_rejected"),
        ("expired", "approval_expired"),
        ("pending", "approval_wait_timed_out"),
        ("stale", "AUTHORIZATION_STALE"),
        ("unavailable", None),
        ("revoked", None),
    ],
)
def test_partial_refund_governance_and_exact_approval_replay(
    framework, mode, outcome, state
):
    from decimal import Decimal

    ControlService.outcome = outcome
    scenario = RefundRequest("ORD-1042", Decimal("100.00"))
    before = scenario.lookup_order("ORD-1042")
    ControlService.pending_check = staticmethod(
        lambda: scenario.lookup_order("ORD-1042") == before
        or pytest.fail("balance changed while pending")
    )
    if state is None:
        with pytest.raises(APIError):
            execute(framework, mode, scenario)
    else:
        result, client = execute(framework, mode, scenario)
        assert result["state"] == state
        assert all(
            request["selected_values"]
            == [{"path": "amount", "value": 100}, {"path": "currency", "value": "EUR"}]
            for request in client.requests
        )
        if outcome == "approved":
            assert (
                len(client.requests) == 2 and client.requests[0] == client.requests[1]
            )
    if state == "receipt_recorded":
        assert len(scenario.receipts) == 1 and scenario.receipts[0]["amount"] == 100
        after = scenario.lookup_order("ORD-1042")
        assert after["refundable_amount"] == 80 and after["refunded_amount"] == 100
        assert not after["refunded"]
    else:
        assert not scenario.receipts and scenario.lookup_order("ORD-1042") == before


@pytest.mark.parametrize("framework", FRAMEWORKS)
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_cli_partial_amount_reaches_native_tool(monkeypatch, capsys, framework, mode):
    import json
    import sys
    from refund_assistant.cli import run_cli

    for key, value in {
        "OPENEIGHT_ENDPOINT": "http://localhost:7777",
        "OPENEIGHT_API_KEY": "ctk_test",
        "OPENEIGHT_AGENT_NAME": "refund",
        "AGENT_MODEL": "openai:test-model",
        "AGENT_MODEL_API_KEY": "explicit-provider-key",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        "refund_assistant.config.init_chat_model", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["runner", "run", "ORD-1041", "--amount", "10.00", "--execution", mode],
    )
    assert (
        run_cli(
            lambda _, scenario, config: framework.build(
                model(scenario), scenario, config
            )
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["state"] == "receipt_recorded"
    assert output["receipts"][0]["amount"] == 10
    assert output["receipts"][0]["currency"] == "USD"


@pytest.mark.parametrize("amount", ["invalid", "0", "-1", "NaN", "Infinity"])
def test_cli_invalid_partial_request_never_constructs_model(monkeypatch, amount):
    import sys
    from unittest.mock import Mock
    from refund_assistant.cli import run_cli

    factory = Mock()
    monkeypatch.setattr("refund_assistant.cli.model_client", factory)
    monkeypatch.setattr(sys, "argv", ["runner", "run", "ORD-1041", "--amount", amount])
    with pytest.raises(SystemExit) as error:
        run_cli(Mock())
    assert error.value.code == 2
    factory.assert_not_called()


def test_cli_over_refund_never_constructs_model(monkeypatch, capsys):
    import json
    import sys
    from unittest.mock import Mock
    from refund_assistant.cli import run_cli

    factory = Mock()
    monkeypatch.setattr("refund_assistant.cli.model_client", factory)
    monkeypatch.setattr(sys, "argv", ["runner", "run", "ORD-1041", "--amount", "25.01"])
    assert run_cli(Mock()) == 1
    assert json.loads(capsys.readouterr().out)["receipts"] == []
    factory.assert_not_called()


def test_cli_publish_rejects_customer_amount(monkeypatch):
    import sys
    from unittest.mock import Mock
    from refund_assistant.cli import run_cli

    factory = Mock()
    monkeypatch.setattr("refund_assistant.cli.model_client", factory)
    monkeypatch.setattr(sys, "argv", ["runner", "publish", "--amount", "10"])
    with pytest.raises(SystemExit) as error:
        run_cli(Mock())
    assert error.value.code == 2
    factory.assert_not_called()
