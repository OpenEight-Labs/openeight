"""The same business request survives migration; governance can block its effect."""

from decimal import Decimal

import pytest
from openeight import BlockedError

import langchain_agent
import native_agent
from control_service import ControlService
from refund_assistant.business import RefundRequest
from test_managed import config, control as control
from test_native import scripted_model


def test_native_and_governed_agents_preserve_the_same_requested_refund():
    requests = [RefundRequest("ORD-1041", Decimal("10.50")) for _ in range(2)]
    native = native_agent.build(scripted_model(requests[0]), requests[0])
    governed = langchain_agent.build(scripted_model(requests[1]), requests[1], config())
    for agent, request in zip((native, governed), requests):
        agent.invoke({"messages": [("user", request.prompt)]})
    assert requests[0].lookup_order("ORD-1041") == requests[1].lookup_order("ORD-1041")
    for request in requests:
        assert len(request.receipts) == 1
        assert request.receipts[0]["amount"] == 10.5
        assert request.receipts[0]["currency"] == "USD"
    assert len(ControlService.instances[-1].requests) == 1


def test_migrated_agent_blocks_the_same_business_tool_when_denied():
    ControlService.outcome = "deny"
    request = RefundRequest("ORD-1041", Decimal("10.50"))
    agent = langchain_agent.build(scripted_model(request), request, config())
    with pytest.raises(BlockedError):
        agent.invoke({"messages": [("user", request.prompt)]})
    assert request.receipts == []
    assert request.lookup_order("ORD-1041")["refundable_amount"] == 25
