import json
import pytest
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from openeight.langchain._callbacks import ObservationCallbacks
from openeight.langchain._content import (
    model_input,
    model_output,
    tool_output,
)
from test_observation_facade import bind
from test_framework_boundary import fixture


def test_public_messages_preserve_roles_tool_calls_and_results_without_provider_metadata():
    reply = AIMessage(
        content="",
        tool_calls=[{"id": "call-1", "name": "refund", "args": {"amount": 180}}],
        additional_kwargs={"authorization": "do-not-capture"},
        response_metadata={"raw": "do-not-capture"},
    )
    result = model_output(LLMResult(generations=[[ChatGeneration(message=reply)]]))
    assert result["generations"][0][0] == {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call-1", "name": "refund", "args": {"amount": 180}}],
    }
    captured = model_input(
        [
            [
                HumanMessage(content="Refund please"),
                reply,
                ToolMessage(content='{"receipt":"r"}', tool_call_id="call-1"),
            ]
        ]
    )
    assert captured["messages"][0][0] == {"role": "user", "content": "Refund please"}
    assert captured["messages"][0][2]["tool_call_id"] == "call-1"
    assert "do-not-capture" not in json.dumps(captured)


def test_callback_captures_start_snapshot_and_final_model_message(bind):
    from openeight._content_capture import validate_content_policy

    owner = bind(fixture()[0])
    owner.content_policy = validate_content_policy("all", None)
    invocation = owner._admit_invocation({}, "invoke")
    callback = ObservationCallbacks(invocation)
    run_id = uuid4()
    message = HumanMessage(content="original request")
    callback.on_chat_model_start({}, [[message]], run_id=run_id)
    message.content = "changed later"
    callback.on_llm_end(
        LLMResult(
            generations=[[ChatGeneration(message=AIMessage(content="final reply"))]]
        ),
        run_id=run_id,
    )
    invocation.finish_execution()
    assert owner.flush(2).pending_records == 0
    event = next(
        e
        for body in owner.client.writes
        for e in body.get("events", [])
        if e["observation_kind"] == "llm_call"
    )
    assert (
        json.loads(event["input_json"])["messages"][0][0]["content"]
        == "original request"
    )
    assert (
        json.loads(event["output_json"])["generations"][0][0]["content"]
        == "final reply"
    )


def test_malformed_projections_fail_closed():
    from openeight._content_capture import capture_optional_content, validate_content_policy
    from openeight import TelemetryContentContext
    context = TelemetryContentContext(kind="llm_call", direction="input")
    assert capture_optional_content(validate_content_policy("all", None), context,
                                    lambda: model_output(object())) == (None, "unsupported_content")


def test_custom_chat_role_is_preserved():
    from langchain_core.messages import ChatMessage

    assert (
        model_input([[ChatMessage(role="developer", content="instructions")]])[
            "messages"
        ][0][0]["role"]
        == "developer"
    )


@pytest.mark.parametrize("mode", ["none", "inputs", "outputs", "all"])
@pytest.mark.parametrize("chat", [False, True])
def test_chat_and_plain_models_capture_only_selected_public_directions(bind, mode, chat):
    from langchain_core.outputs import Generation
    from openeight._content_capture import validate_content_policy
    owner = bind(fixture()[0])
    owner.content_policy = validate_content_policy(mode, None)
    invocation = owner._admit_invocation({}, "invoke")
    callback, run = ObservationCallbacks(invocation), uuid4()
    if chat:
        callback.on_chat_model_start({}, [[HumanMessage(content="MODEL-SECRET")]], run_id=run)
        response = LLMResult(generations=[[ChatGeneration(message=AIMessage(
            content="MODEL-SECRET", response_metadata={"raw": "PROVIDER-SECRET"},
        ))]])
    else:
        callback.on_llm_start({}, ["MODEL-SECRET"], run_id=run)
        response = LLMResult(generations=[[Generation(text="MODEL-SECRET")]], llm_output={"raw": "PROVIDER-SECRET"})
    callback.on_llm_new_token("STREAM-SECRET", run_id=run)
    callback.on_llm_end(response, run_id=run)
    invocation.finish_execution()
    owner.flush(2)
    event, = [e for body in owner.client.writes for e in body.get("events", [])]
    assert ("input_json" in event) == (mode in ("inputs", "all"))
    assert ("output_json" in event) == (mode in ("outputs", "all"))
    assert "PROVIDER-SECRET" not in json.dumps(event) and "STREAM-SECRET" not in json.dumps(event)
    if mode in ("inputs", "all"):
        assert "MODEL-SECRET" in event["input_json"]
    if mode in ("outputs", "all"):
        assert "MODEL-SECRET" in event["output_json"]
