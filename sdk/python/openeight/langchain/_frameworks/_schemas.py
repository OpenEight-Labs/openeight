"""Qualified native public schema projection."""

from openeight.errors import ConfigurationError


def tool_schema(tool):
    schema = tool.tool_call_schema
    if isinstance(schema, type):
        schema = (
            schema.model_json_schema()
            if hasattr(schema, "model_json_schema")
            else schema.schema()
        )
    if type(schema) is not dict:
        raise ConfigurationError("unsupported_tool_definition") from None
    return schema
