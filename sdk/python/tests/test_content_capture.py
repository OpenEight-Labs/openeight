"""Opt-in disclosure, strict JSON limits and application hook isolation."""

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from openeight import (
    ConfigurationError,
    DeliveryStatus,
    Governance,
    TelemetryContentContext,
)
from openeight._content_capture import capture_optional_content, validate_content_policy


def context(direction="input"):
    return TelemetryContentContext(kind="llm_call", direction=direction)


def capture(value, mode="all", hook=None, direction="input"):
    return capture_optional_content(
        validate_content_policy(mode, hook), context(direction), lambda: value
    )


@pytest.mark.parametrize("mode", [None, True, False, "ALL", "input", "", [], {}])
def test_invalid_modes_fail_at_construction(mode):
    with pytest.raises(ConfigurationError) as error:
        Governance(
            agent_name="agent",
            endpoint="http://localhost:7777",
            credential="ctk_test",
            capture_content=mode,
        )
    assert error.value.reason == "invalid_telemetry_content_configuration"


def test_noncallable_redactor_and_immutable_public_values():
    with pytest.raises(ConfigurationError):
        validate_content_policy("none", True)
    with pytest.raises(FrozenInstanceError):
        context().direction = "output"
    status = DeliveryStatus()
    assert set(status.content_capture_failures.values()) == {0}
    with pytest.raises(TypeError):
        status.content_capture_failures["redaction_failed"] = 9
    assert (
        DeliveryStatus().content_capture_failures is not status.content_capture_failures
    )


@pytest.mark.parametrize("mode", ["none", "inputs", "outputs", "all"])
@pytest.mark.parametrize("direction", ["input", "output"])
def test_directions_checked_before_projection_or_hook(mode, direction):
    calls = []

    def project():
        calls.append("project")
        return "marker"

    def hook(ctx, value):
        assert ctx == context(direction)
        calls.append("hook")
        return "[REDACTED]"

    value, reason = capture_optional_content(
        validate_content_policy(mode, hook), context(direction), project
    )
    enabled = mode in ("all", direction + "s")
    assert calls == (["project", "hook"] if enabled else [])
    assert (value, reason) == (("[REDACTED]", None) if enabled else (None, None))


def test_both_hook_input_and_result_are_detached_and_none_suppresses():
    original, replacement = {"content": ["secret"]}, {"safe": []}

    def hook(ctx, value):
        value["content"].clear()
        return replacement

    snapshot, reason = capture(original, hook=hook)
    replacement["safe"].append("later")
    assert original == {"content": ["secret"]}
    assert snapshot == {"safe": []} and reason is None
    assert capture(original, hook=lambda *_: None) == (None, None)


@pytest.mark.parametrize("value", [object(), {1: "secret"}, float("nan"), float("inf")])
def test_unsupported_and_invalid_redaction_have_distinct_reasons(value):
    assert capture(value) == (None, "unsupported_content")
    assert capture("safe", hook=lambda *_: value) == (None, "invalid_redaction_result")


def test_limits_are_canonical_utf8_and_applied_before_and_after_hook():
    assert capture("é" * 8191) == ("é" * 8191, None)
    assert capture("é" * 8192) == (None, "payload_limit")
    assert capture("x" * 70000) == (None, "payload_limit")
    assert capture("small", hook=lambda *_: "x" * 16383) == (None, "payload_limit")

    def forbidden(*args):
        pytest.fail("oversized source reached hook")

    assert capture("x" * 16383, hook=forbidden) == (None, "payload_limit")
    cycle = []
    cycle.append(cycle)
    assert capture(cycle) == (None, "unsupported_content")


def test_hook_exception_and_awaitable_suppress_without_exception_text():
    def failure(*_):
        raise RuntimeError("secret")

    async def async_hook(*_):
        return "secret"

    assert capture("value", hook=failure) == (None, "redaction_failed")
    assert capture("value", hook=async_hook) == (None, "invalid_redaction_result")


@pytest.mark.parametrize(
    "signal", [asyncio.CancelledError, KeyboardInterrupt, SystemExit]
)
def test_termination_signals_propagate(signal):
    def terminate(*_):
        raise signal

    with pytest.raises(signal):
        capture("value", hook=terminate)
