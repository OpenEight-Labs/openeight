"""Native Command output capture excludes unrelated graph state."""

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from openeight.langchain._content import tool_output


def test_command_output_captures_only_one_tool_message_and_never_graph_state():
    from langgraph.types import Command

    value = Command(
        update={
            "messages": [ToolMessage(content="receipt", tool_call_id="call-1")],
            "files": {"private": "never-capture"},
            "todos": ["never-capture"],
        }
    )
    assert tool_output(value) == {
        "role": "tool",
        "content": "receipt",
        "tool_call_id": "call-1",
    }
    assert value.update["files"] == {"private": "never-capture"}


@pytest.mark.parametrize(
    "update",
    [
        None,
        {},
        {"messages": []},
        {"messages": [HumanMessage(content="private")]},
        {
            "messages": [
                ToolMessage(content="one", tool_call_id="one"),
                ToolMessage(content="two", tool_call_id="two"),
            ]
        },
        [("messages", [ToolMessage(content="private", tool_call_id="call")])],
    ],
)
def test_ambiguous_command_outputs_fail_closed(update):
    from langgraph.types import Command

    from openeight import TelemetryContentContext
    from openeight._content_capture import (
        capture_optional_content,
        validate_content_policy,
    )

    assert capture_optional_content(
        validate_content_policy("all", None),
        TelemetryContentContext(kind="tool_call", direction="output"),
        lambda: tool_output(Command(update=update)),
    ) == (None, "unsupported_content")
