"""Opt-in demonstration redaction, independent of required policy evidence."""

from openeight import OpenEightConfig
from openeight.langchain import ApprovalConfig
from openeight.content import JSONValue, TelemetryContentContext
from contracts import Settings

SENTINEL = "INCIDENT_DEMO_PRIVATE_SENTINEL"
CONTROL = "INCIDENT_DEMO_CONTROL"


def redact_synthetic_content(
    context: TelemetryContentContext, value: JSONValue
) -> JSONValue | None:
    if isinstance(value, str):
        return value.replace(SENTINEL, "[REDACTED]")
    if isinstance(value, list):
        return [redact_synthetic_content(context, item) for item in value]
    if isinstance(value, dict):
        return {
            key: redact_synthetic_content(context, item) for key, item in value.items()
        }
    return value


def build_configuration(*, settings: Settings) -> OpenEightConfig:
    return OpenEightConfig(
        endpoint=settings.endpoint,
        agent_name=settings.agent_name,
        credential=settings.credential,
        on_block="raise",
        approval=ApprovalConfig(timeout=settings.approval_timeout_seconds),
        capture_content=settings.capture_content,
        redact_content=redact_synthetic_content
        if settings.redaction == "synthetic-sentinels"
        else None,
    )
