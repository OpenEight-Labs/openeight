"""Public schema, provider configuration and packaging contracts."""

from pathlib import Path
import tomllib

from retailer import FIELDS, KINDS
from tools import build_tools


def test_all_public_schemas_hide_keys_and_reject_extra_properties():
    for tool in build_tools(None):
        schema = tool.tool_call_schema.model_json_schema()
        assert set(schema["properties"]) == set(FIELDS[tool.name])
        assert "idempotency_key" not in schema["properties"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(FIELDS[tool.name])
        assert ("idempotency_key" in tool.get_input_schema().model_fields) == (tool.name in KINDS)


def test_customer_support_requires_pinned_async_sqlite_framework():
    project = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )["project"]
    assert project["requires-python"] == ">=3.11,<3.15"
    for dependency in (
        "openeight[langchain-durable]==0.0.1",
        "langchain==1.4.3",
        "langchain-core==1.6.7",
        "langgraph==1.2.14",
        "langgraph-prebuilt==1.1.0",
        "langgraph-checkpoint==4.3.0",
        "langgraph-checkpoint-sqlite==3.1.2",
        "aiosqlite==0.22.1",
    ):
        assert dependency in project["dependencies"]


def test_tool_descriptions_publish_business_facts():
    from retailer import ACTIONS

    for tool in build_tools(None):
        assert "Connector: retailer." in tool.description
        assert f"Action: {ACTIONS[tool.name]}." in tool.description
        assert "Capability:" not in tool.description
    refund = next(tool for tool in build_tools(None) if tool.name == "issue_refund")
    assert "major currency units" in refund.description
    assert "Decimal amounts are supported" in refund.description
    assert "not an integer-only operation" in refund.description
    assert "whole currency units" not in refund.description
    amount = refund.tool_call_schema.model_json_schema()["properties"]["amount"]
    assert "Decimal amounts are supported" in amount["description"]
    assert {option["type"] for option in amount["anyOf"]} == {"integer", "number"}
    assert "USD" not in refund.description


def test_strict_numeric_and_required_nullable_native_contracts():
    import pytest
    from pydantic import ValidationError
    tools = {t.name: t for t in build_tools(None)}
    for value in (50, 50.0, 50.25):
        args = {"action_id": "action", "facts_revision": 1, "amount": value, "currency": "USD"}
        actual = tools["issue_refund"].tool_call_schema.model_validate(args, strict=True).model_dump()
        assert type(actual["amount"]) is type(value)
    for value in (True, "50", float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            tools["issue_refund"].tool_call_schema.model_validate({"action_id": "action", "facts_revision": 1, "amount": value, "currency": "USD"}, strict=True)
    assert "send_customer_reply" not in tools
    resolution = tools["resolve_case"].tool_call_schema
    assert set(resolution.model_json_schema()["required"]) == {"case_id", "action_id", "facts_revision"}
    with pytest.raises(ValidationError):
        resolution.model_validate({"case_id": "case", "action_id": None, "facts_revision": 1})
    assert "close_case" not in tools and "propose_resolution" not in tools
