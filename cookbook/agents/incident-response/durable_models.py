"""Scripted durable consumer reads the real artifact before returning its report."""

from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from pydantic import Field

from models import ModelSet, ScriptedModel, build_models, call
from reporting import ARTIFACT, ReportError, parse_report


def observed_artifact(content):
    if type(content) is not str or "\n" not in content:
        raise ReportError("report_missing")
    header, body = content.split("\n", 1)
    if (
        not header.startswith("@@ ")
        or not header.endswith(" @@")
        or "truncated" in header
    ):
        raise ReportError("report_malformed")
    parse_report(body)
    return body


class DurableScriptedModel(ScriptedModel):
    destination: Any = Field(exclude=True, repr=False)

    def _next(self, messages):
        response = super()._next(messages)
        if self.role == "reporting" and not response.tool_calls:
            parse_report(response.content)
            # Retain actual child output as local corroboration, never a replacement artifact.
            self.destination.record_report(response.content)
        if self.role == "coordinator" and not response.tool_calls:
            reads = [
                m
                for m in messages
                if isinstance(m, ToolMessage) and m.name == "read_file"
            ]
            if not reads:
                return call("read_file", file_path=ARTIFACT)
            if reads[-1].status != "success":
                raise ReportError("report_missing")
            body = observed_artifact(reads[-1].content)
            if not self.destination.has_report(body):
                raise ReportError("report_mismatch")
            return AIMessage(content=body)
        return response


def durable_models(*, settings, scenario, destination):
    if settings.mode != "scripted" or settings.profile != "default":
        raise ValueError("unsupported_durable_example_configuration")
    models = build_models(settings=settings, scenario=scenario)
    return ModelSet(
        *(
            DurableScriptedModel(
                role=model.role,
                scenario=model.scenario,
                action_id=model.action_id,
                name=model.name,
                destination=destination,
            )
            for model in (
                models.coordinator,
                models.diagnostics,
                models.remediation,
                models.reporting,
            )
        )
    )
