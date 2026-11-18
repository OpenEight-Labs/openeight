"""Public, framework-independent application telemetry redaction context."""

from dataclasses import dataclass
from typing import Literal

JSONValue = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]


@dataclass(frozen=True, kw_only=True)
class TelemetryContentContext:
    """Identify the optional content field supplied to an application redactor."""

    kind: Literal["llm_call", "tool_call"]
    direction: Literal["input", "output"]
