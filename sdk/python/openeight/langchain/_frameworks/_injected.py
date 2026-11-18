"""Restore a frozen SDK argument after ToolNode strips model-supplied keys."""

import json
from typing import Annotated, get_args, get_origin

from langchain_core.tools import BaseTool, InjectedToolArg
from pydantic import PrivateAttr

from openeight import ConfigurationError


def hidden_action_key(tool):
    """Accept only the explicit native string annotation supported by durable tools."""
    schema = tool.get_input_schema()
    fields = schema.model_fields
    public = tool.tool_call_schema.model_json_schema()
    full = schema.model_json_schema()
    injected = set(full.get("properties", {})) - set(public.get("properties", {}))
    if not injected:
        return False
    annotations = __import__("typing").get_type_hints(schema, include_extras=True)
    annotation = annotations.get("idempotency_key")
    if (
        injected != {"idempotency_key"}
        or get_origin(annotation) is not Annotated
        or get_args(annotation) != (str, InjectedToolArg)
        or "idempotency_key" not in fields
    ):
        raise ConfigurationError("unsupported_tool_definition")
    public = tool.tool_call_schema.model_json_schema()
    full = schema.model_json_schema()
    if "idempotency_key" in public.get("properties", {}) or set(
        full.get("properties", {})
    ) - {"idempotency_key"} != set(public.get("properties", {})):
        raise ConfigurationError("unsupported_framework_version")
    return True


class FrozenInjectedTool(BaseTool):
    """A single-attempt native handler proxy; never registered or exposed to models."""

    _original: BaseTool = PrivateAttr()
    _native: bytes = PrivateAttr()
    _consumed: bool = PrivateAttr(default=False)

    def __init__(self, original, native):
        if not hidden_action_key(original):
            raise ConfigurationError("unsupported_tool_definition")
        super().__init__(
            name=original.name,
            description=original.description,
            args_schema=original.args_schema,
        )
        self._original = original
        self._native = bytes(native)

    def _run(self, *args, **kwargs):
        raise ConfigurationError("unsupported_execution_method")

    async def ainvoke(self, input, config=None, **kwargs):
        frozen = json.loads(self._native)
        public = {
            **frozen,
            "args": {k: v for k, v in frozen["args"].items() if k != "idempotency_key"},
        }
        try:
            received = json.dumps(
                input, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
            expected = json.dumps(
                public, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except (TypeError, ValueError):
            raise ConfigurationError("invalid_input") from None
        if received != expected:
            raise ConfigurationError("unsupported_tool_definition")
        if self._consumed:
            raise ConfigurationError("durable_attempt_already_invoked")
        self._consumed = True
        # Only the captured original tool invokes callbacks and its handler. The
        # native ToolNode remains responsible for execution/error normalization.
        return await self._original.ainvoke(frozen, config, **kwargs)
