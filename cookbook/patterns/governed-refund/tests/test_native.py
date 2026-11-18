"""Native baseline runs the actual LangChain graph without OpenEight configuration."""

from decimal import Decimal
import json

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

import native_agent
from refund_assistant.business import RefundRequest


class ToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def scripted_model(request):
    def call(name, arguments):
        return AIMessage(
            content="", tool_calls=[{"name": name, "args": arguments, "id": name}]
        )

    return ToolModel(
        responses=[
            call("lookup_order", {"order_id": request.order.order_id}),
            call(
                "issue_refund",
                {
                    "order_id": request.order.order_id,
                    "amount": request.json_requested_amount,
                    "currency": request.order.currency,
                },
            ),
            AIMessage(content="The simulated refund receipt was recorded."),
        ]
    )


def test_native_cli_uses_only_model_configuration(monkeypatch, capsys):
    for name in ("OPENEIGHT_ENDPOINT", "OPENEIGHT_AGENT_NAME", "OPENEIGHT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    request = RefundRequest("ORD-1041", Decimal("10.50"))
    monkeypatch.setattr(native_agent, "model_client", lambda: scripted_model(request))
    assert native_agent.main(["ORD-1041", "--amount", "10.50"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["governance"] == "not_enabled"
    assert result["receipts"][0]["amount"] == 10.5
    assert result["receipts"][0]["simulated"] is True


def test_native_model_prose_without_a_receipt_is_not_success(monkeypatch, capsys):
    monkeypatch.setattr(
        native_agent,
        "model_client",
        lambda: ToolModel(responses=[AIMessage(content="Refund completed.")]),
    )
    assert native_agent.main(["ORD-1041"]) == 1
    assert json.loads(capsys.readouterr().out)["receipts"] == []
