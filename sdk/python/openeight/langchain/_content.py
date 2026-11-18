"""Bounded public message/tool projections; never serialize provider objects or repr."""

from langchain_core.messages import AIMessage, BaseMessage, ChatMessage, ToolMessage
from langgraph.types import Command


def message_content(message):
    if not isinstance(message, BaseMessage):
        raise ValueError("unsupported_content")
    result = {
        "role": message.role
        if isinstance(message, ChatMessage)
        else {"human": "user", "ai": "assistant"}.get(message.type, message.type),
        "content": message.content,
    }
    if message.name:
        result["name"] = message.name
    if message.type == "tool":
        result["tool_call_id"] = message.tool_call_id
    if isinstance(message, AIMessage) and message.tool_calls:
        result["tool_calls"] = [
            {key: call[key] for key in ("id", "name", "args") if key in call}
            for call in message.tool_calls
        ]
    return result


def model_input(messages):
    return {"messages": [[message_content(m) for m in batch] for batch in messages]}


def model_output(response):
    return {
        "generations": [
            [
                message_content(g.message)
                if hasattr(g, "message")
                else {"text": g.text}
                for g in batch
            ]
            for batch in response.generations
        ]
    }


def tool_output(value):
    if isinstance(value, Command):
        # A tool result may carry graph mutations; only its one response is content.
        if type(value.update) is not dict:
            raise ValueError("unsupported_content")
        messages = value.update.get("messages")
        if (
            type(messages) not in (list, tuple)
            or len(messages) != 1
            or not isinstance(messages[0], ToolMessage)
        ):
            raise ValueError("unsupported_content")
        return message_content(messages[0])
    return message_content(value) if isinstance(value, BaseMessage) else value
