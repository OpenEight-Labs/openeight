"""Native conversation continuation keeps consent separate from text."""

import asyncio
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from local import LocalError
from service import DemoService, load_binding
from initialization import setup, write_private
from retailer import FIELDS, KINDS
from helpers_remedies import confirm


class ConversationModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def test_single_agent_reviews_duplicate_charge_and_hands_off_without_refund(
    tmp_path, monkeypatch, offline_control
):
    state = prepare(tmp_path, monkeypatch)

    def call(name, **args):
        return AIMessage(
            content="", tool_calls=[{"id": str(uuid4()), "name": name, "args": args}]
        )

    model = ConversationModel(
        responses=[
            call("get_payment", order_id="order_billing"),
            call(
                "escalate_case",
                case_id="case_billing",
                reason="possible_duplicate_charge",
            ),
            AIMessage(content="Your case has been handed to billing for review."),
        ]
    )
    service = DemoService(state, model_factory=lambda: model)
    accepted = command(service, "I was charged twice", case="case_billing")
    result = asyncio.run(service.execute(accepted["command_id"]))
    assert result["case"]["status"] == "handed_off"
    assert result["status"] == "completed"
    assert result["turns"][-1]["body"] == "Your case has been handed to billing for review."
    assert [row["kind"] for row in result["receipts"]].count("escalation") == 1
    assert not any(row["kind"] == "refund" for row in result["receipts"])


def prepare(tmp_path, monkeypatch):
    for key in (
        "OPENEIGHT_ENDPOINT",
        "OPENEIGHT_API_KEY",
        "OPENEIGHT_AGENT_NAME",
        "OPENEIGHT_CONSOLE_ORIGIN",
    ):
        monkeypatch.delenv(key, raising=False)
    state = tmp_path / "standalone"
    setup(state, deployment_id="local", mode="standalone", apply=True)
    return state


def command(service, message, thread=None, case="case_damaged"):
    return service.accept(
        {
            "kind": "reply" if thread else "start",
            "command_id": str(uuid4()),
            "message": message,
            **({"thread_id": thread} if thread else {"case_id": case}),
        }
    )


def save_confirmation(service, thread, kind="refund"):
    view = service.snapshot(thread)
    payload = {"remedy": kind, "reason": "I confirm the displayed whole-order remedy"}
    if kind == "refund":
        payload.update(
            amount=view["case"]["refundable_amount"], currency=view["case"]["currency"]
        )
    if kind == "replacement":
        payload["shipping_method"] = "standard"
    return service.control(
        {
            "command_id": str(uuid4()),
            "thread_id": thread,
            "case_id": view["case"]["case_id"],
            "facts_revision": view["case"]["facts_revision"],
            "kind": "confirmation",
            "payload": payload,
        }
    )


def test_standalone_intake_without_openeight(tmp_path, monkeypatch, offline_control):
    state = prepare(tmp_path, monkeypatch)
    model = ConversationModel(
        responses=[
            AIMessage(content="What happened with your order?"),
            AIMessage(content="Would you prefer a replacement or a refund?"),
        ]
    )
    service = DemoService(state, model_factory=lambda: model)
    first = command(service, "I need help")
    view = asyncio.run(service.execute(first["command_id"]))
    assert view["mode"] == "standalone" and view["run_id"] is None and view["can_reply"]
    second = command(service, "The item is damaged.", view["thread_id"])
    view = asyncio.run(service.execute(second["command_id"]))
    assert [t["body"] for t in view["turns"]] == [
        "I need help",
        "What happened with your order?",
        "The item is damaged.",
        "Would you prefer a replacement or a refund?",
    ]
    assert not view["activity"] and not view["receipts"]
    assert offline_control.calls == []


def test_one_support_agent_reads_order_and_shipment_then_executes_confirmed_refund(
    tmp_path, monkeypatch
):
    state = prepare(tmp_path, monkeypatch)
    model = ConversationModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "order",
                        "name": "get_order",
                        "args": {"order_id": "order_small"},
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "shipment",
                        "name": "get_shipment",
                        "args": {"order_id": "order_small"},
                    }
                ],
            ),
            AIMessage(content="Please review the refund in the confirmation control."),
        ]
    )
    service = DemoService(state, model_factory=lambda: model)
    started = command(service, "Please refund this damaged order.", case="case_small")
    view = asyncio.run(service.execute(started["command_id"]))
    assert not view["receipts"]
    reads = service.store.local_attempts(view["thread_id"])
    assert {attempt.tool_name for attempt in reads} == {"get_order", "get_shipment"}
    assert all(attempt.idempotency_key is None for attempt in reads)
    result = save_confirmation(service, view["thread_id"])
    assert service.store.local_attempts(view["thread_id"]) == reads
    r = service.retailer("case_small")
    action_id = result["action_id"]
    service.model_factory = lambda: ConversationModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "refund",
                        "name": "issue_refund",
                        "args": r.execution_arguments(action_id),
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "reply",
                        "name": "resolve_case",
                        "args": {
                            "case_id": "case_small",
                            "action_id": action_id,
                            "facts_revision": r.execution_arguments(action_id)["facts_revision"] + 1,
                        },
                    }
                ],
            ),
            AIMessage(content="Your refund is complete."),
        ]
    )
    next_id = str(uuid4())
    accepted = service.continue_conversation(view["thread_id"], next_id)
    assert service.store.find_command(next_id).kind == "reply"
    completed = asyncio.run(service.execute(accepted["command_id"]))
    assert (
        completed["status"] == "completed" and completed["case"]["status"] == "resolved"
    )
    assert [r["kind"] for r in completed["receipts"]].count("refund") == 1
    assert (
        service.continue_conversation(view["thread_id"], next_id)["command_id"]
        == next_id
    )
    assert (
        service.store.attempts(view["thread_id"]) == ()
        and service.store.pending(view["thread_id"]) == ()
    )


@pytest.mark.parametrize("name", list(FIELDS))
def test_every_standalone_native_tool(tmp_path, monkeypatch, name):
    state = prepare(tmp_path, monkeypatch)
    service = DemoService(state)
    r = service.retailer("case_small")
    kind = KINDS.get(name)
    if kind in ("refund", "replacement", "return", "cancellation"):
        action = confirm(r, kind)
        args = r.execution_arguments(action)
    elif kind == "closure":
        action = confirm(r)
        r.mutate("issue_refund", r.execution_arguments(action), str(uuid4()))
        args = {"case_id": r.context.case_id, "action_id": action,
                "facts_revision": r.read("get_order", {"order_id": r.context.order_id})["facts_revision"]}
    else:
        fields = {
            "case_id": "case_small",
            "order_id": "order_small",
            "query": "How does the workflow proceed?",
            "body": "Support update recorded",
            "reason": "Customer requested support",
            "action_id": None,
        }
        args = {key: fields[key] for key in FIELDS[name]}
    service.model_factory = lambda: ConversationModel(
        responses=[
            AIMessage(
                content="", tool_calls=[{"id": "action", "name": name, "args": args}]
            ),
            AIMessage(content="Support update recorded."),
        ]
    )
    accepted = command(service, "Please help with the order.", case="case_small")
    view = asyncio.run(service.execute(accepted["command_id"]))
    attempts = service.store.local_attempts(view["thread_id"])
    assert len(attempts) == 1 and attempts[0].status == "succeeded"
    assert service.store.pending(view["thread_id"]) == ()


def test_standalone_crash_after_receipt_restores_original_action(tmp_path, monkeypatch):
    state = prepare(tmp_path, monkeypatch)
    service = DemoService(state)
    r = service.retailer("case_small")
    action = confirm(r)
    refund = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "refund",
                "name": "issue_refund",
                "args": r.execution_arguments(action),
            }
        ],
    )
    service.model_factory = lambda: ConversationModel(
        responses=[refund, AIMessage(content="Refund confirmed.")]
    )
    accepted = command(service, "Refund the damaged item.", case="case_small")
    injected = False

    def crash(point):
        nonlocal injected
        if point == "after_local_attempt_result" and not injected:
            injected = True
            raise RuntimeError("crash")

    service.store.fault = crash
    with pytest.raises(RuntimeError):
        asyncio.run(service.execute(accepted["command_id"]))
    old = service.store.local_attempts(accepted["thread_id"])[0]
    assert old.status == "executing"
    restarted = DemoService(
        state,
        model_factory=lambda: ConversationModel(
            responses=[AIMessage(content="Refund confirmed.")]
        ),
    )
    resume = restarted.continue_conversation(accepted["thread_id"], str(uuid4()))
    assert restarted.store.find_command(resume["command_id"]).kind == "resume"
    view = asyncio.run(restarted.execute(resume["command_id"]))
    current = restarted.store.local_attempts(accepted["thread_id"])[0]
    assert (current.id, current.idempotency_key) == (
        old.id,
        old.idempotency_key,
    ) and current.status == "succeeded"
    assert len([x for x in view["receipts"] if x["kind"] == "refund"]) == 1


def test_setup_has_no_human_session_or_governance(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx.Client,
        "send",
        lambda *a, **k: pytest.fail("Setup made a network request"),
    )
    assert (
        setup(tmp_path / "local", deployment_id="local", apply=True)["mode"]
        == "standalone"
    )
    with pytest.raises(LocalError, match="setup_state_exists"):
        setup(tmp_path / "local", deployment_id="local", apply=True)


def test_integrated_setup_pins_supplied_key_without_network(tmp_path, monkeypatch):
    import httpx

    keyfile = tmp_path / "key"
    write_private(keyfile, "ctk_test_supplied")
    monkeypatch.setattr(
        httpx.Client,
        "send",
        lambda *a, **k: pytest.fail("Setup made a network request"),
    )
    state = tmp_path / "integrated"
    setup(
        state,
        deployment_id="integrated",
        mode="integrated",
        endpoint="http://localhost:7777",
        agent_name="new-agent",
        api_key_file=keyfile,
        apply=True,
    )
    d, key = load_binding(state, runtime=True)
    assert d.schema_version == 4 and key == "ctk_test_supplied"
    monkeypatch.setenv("OPENEIGHT_API_KEY", "ctk_replacement")
    with pytest.raises(LocalError, match="credential_rotation_refused"):
        load_binding(state, runtime=True)


def test_standalone_content_blocks_publish_only_visible_text(tmp_path, monkeypatch):
    state = prepare(tmp_path, monkeypatch)
    model = ConversationModel(
        responses=[
            AIMessage(
                content=[
                    {"type": "reasoning", "reasoning": "private canary"},
                    {"type": "text", "text": "What happened?"},
                ]
            )
        ]
    )
    service = DemoService(state, model_factory=lambda: model)
    admitted = command(service, "I need help")
    view = asyncio.run(service.execute(admitted["command_id"]))
    assert view["turns"][-1][
        "body"
    ] == "What happened?" and "private canary" not in str(view)


def test_cli_setup_reads_agent_key_from_environment(tmp_path, monkeypatch, capsys):
    from cli import main

    key = "ctk_test_environment"
    monkeypatch.setenv("OPENEIGHT_API_KEY", key)
    monkeypatch.delenv("OPENEIGHT_ENDPOINT", raising=False)
    monkeypatch.delenv("OPENEIGHT_AGENT_NAME", raising=False)
    state = tmp_path / "environment"
    assert (
        main(
            [
                "--state",
                str(state),
                "setup",
                "--deployment",
                "environment",
                "--mode",
                "integrated",
                "--endpoint",
                "http://localhost:7777",
                "--agent-name",
                "new-agent",
                "--apply",
            ]
        )
        == 0
    )
    assert key not in capsys.readouterr().out
    assert load_binding(state, runtime=True)[1] == key
    assert (state / "machine-key").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("key", [None, "", "invalid-key"])
def test_cli_setup_rejects_missing_or_invalid_environment_key(
    tmp_path, monkeypatch, capsys, key
):
    from cli import main

    monkeypatch.delenv("OPENEIGHT_API_KEY", raising=False)
    if key is not None:
        monkeypatch.setenv("OPENEIGHT_API_KEY", key)
    state = tmp_path / "invalid"
    assert (
        main(
            [
                "--state",
                str(state),
                "setup",
                "--deployment",
                "invalid",
                "--mode",
                "integrated",
                "--endpoint",
                "http://localhost:7777",
                "--agent-name",
                "new-agent",
                "--apply",
            ]
        )
        != 0
    )
    assert not (state / "machine-key").exists()
    assert not (state / "ready").exists()
    if key:
        assert key not in capsys.readouterr().out


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_model_passes_explicit_key_to_provider(monkeypatch, provider):
    from unittest.mock import Mock

    service = object.__new__(DemoService)
    service.model_factory = None
    monkeypatch.setenv("AGENT_MODEL", f"{provider}:test-model")
    monkeypatch.setenv("AGENT_MODEL_API_KEY", "model-provider-key")
    monkeypatch.setenv("OPENEIGHT_API_KEY", "ctk_unrelated_agent_key")
    init = Mock()
    monkeypatch.setattr("agent.init_chat_model", init)
    assert service._model() is init.return_value
    init.assert_called_once_with(
        "test-model", model_provider=provider, api_key="model-provider-key"
    )


@pytest.mark.parametrize("key", [None, ""])
def test_model_leaves_native_auth_available_without_api_key(monkeypatch, key):
    from unittest.mock import Mock

    service = object.__new__(DemoService)
    service.model_factory = None
    monkeypatch.setenv("AGENT_MODEL", "bedrock:test-model")
    monkeypatch.delenv("AGENT_MODEL_API_KEY", raising=False)
    if key is not None:
        monkeypatch.setenv("AGENT_MODEL_API_KEY", key)
    init = Mock()
    monkeypatch.setattr("agent.init_chat_model", init)
    service._model()
    init.assert_called_once_with("test-model", model_provider="bedrock")


@pytest.mark.parametrize(
    "override, expected",
    [
        (None, "https://console.example:8443"),
        ("", "https://console.example:8443"),
        ("http://localhost:4175/", "http://localhost:4175"),
    ],
)
def test_console_origin_links_default_to_saved_endpoint(
    tmp_path, monkeypatch, override, expected
):
    monkeypatch.delenv("OPENEIGHT_CONSOLE_ORIGIN", raising=False)
    if override is not None:
        monkeypatch.setenv("OPENEIGHT_CONSOLE_ORIGIN", override)
    state = tmp_path / "integrated"
    setup(
        state,
        deployment_id="integrated",
        mode="integrated",
        endpoint="https://console.example:8443/",
        agent_name="new-agent",
        api_key="ctk_test_supplied",
        apply=True,
    )
    assert DemoService(state).console_origin == expected
    assert (
        DemoService(state, console_origin="https://explicit.example").console_origin
        == "https://explicit.example"
    )


def test_console_origin_override_still_rejects_invalid_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENEIGHT_CONSOLE_ORIGIN", "https://console.example/not-an-origin")
    state = tmp_path / "integrated"
    setup(
        state,
        deployment_id="integrated",
        mode="integrated",
        endpoint="https://console.example",
        agent_name="new-agent",
        api_key="ctk_test_supplied",
        apply=True,
    )
    with pytest.raises(LocalError, match="invalid_console_origin"):
        DemoService(state)


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("key", [None, "", "   "])
def test_api_key_model_rejects_ambient_credentials(monkeypatch, provider, key):
    from unittest.mock import Mock

    service = object.__new__(DemoService)
    service.model_factory = None
    monkeypatch.setenv("AGENT_MODEL", f"{provider}:test-model")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key-must-not-be-used")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key-must-not-be-used")
    monkeypatch.delenv("AGENT_MODEL_API_KEY", raising=False)
    if key is not None:
        monkeypatch.setenv("AGENT_MODEL_API_KEY", key)
    factory = Mock()
    monkeypatch.setattr("agent.init_chat_model", factory)
    with pytest.raises(LocalError, match="model_api_key_required"):
        service._model()
    factory.assert_not_called()


@pytest.mark.parametrize("flags", [False, True])
def test_documented_setup_resolves_environment_with_flag_precedence(
    tmp_path, monkeypatch, capsys, flags
):
    from cli import main

    monkeypatch.setenv("OPENEIGHT_ENDPOINT", "http://localhost:7777")
    monkeypatch.setenv("OPENEIGHT_AGENT_NAME", "environment-agent")
    monkeypatch.setenv("OPENEIGHT_API_KEY", "ctk_test_environment")
    state = tmp_path / "setup"
    args = ["--state", str(state), "setup", "--deployment", "local-support",
            "--mode", "integrated", "--apply"]
    if flags:
        args += ["--endpoint", "http://localhost:8888", "--agent-name", "flag-agent"]
    assert main(args) == 0
    binding = load_binding(state)
    assert binding.endpoint == ("http://localhost:8888" if flags else "http://localhost:7777")
    assert binding.agent_name == ("flag-agent" if flags else "environment-agent")
    assert "ctk_test_environment" not in capsys.readouterr().out


def test_standalone_setup_ignores_integrated_environment(tmp_path, monkeypatch):
    from cli import main

    monkeypatch.setenv("OPENEIGHT_ENDPOINT", "http://localhost:7777")
    monkeypatch.setenv("OPENEIGHT_AGENT_NAME", "environment-agent")
    monkeypatch.setenv("OPENEIGHT_API_KEY", "ctk_test_environment")
    state = tmp_path / "setup"
    assert main(["--state", str(state), "setup", "--deployment", "local", "--apply"]) == 0
    assert load_binding(state).endpoint is None
    assert not (state / "machine-key").exists()


def test_direct_runtime_resolves_public_model_settings(tmp_path, monkeypatch):
    from unittest.mock import Mock

    state = prepare(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENT_MODEL", "openai:public-model")
    monkeypatch.setenv("AGENT_MODEL_API_KEY", "public-model-key")
    init = Mock()
    monkeypatch.setattr("agent.init_chat_model", init)
    DemoService(state)._model()
    init.assert_called_once_with(
        "public-model", model_provider="openai", api_key="public-model-key"
    )
