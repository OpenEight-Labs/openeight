"""Native response strategies with separate executable and durable result contracts."""

import json
from dataclasses import dataclass, is_dataclass
from functools import partial
from inspect import getclosurevars
from typing import Any

from langchain.agents.structured_output import (
    AutoStrategy,
    ProviderStrategy,
    StructuredOutputError,
    ToolStrategy,
)
from pydantic import BaseModel, TypeAdapter

from openeight._json import copy_json_value, snapshot_json
from openeight.errors import ConfigurationError

from ._measurement import payload_hash, qualified

ResponseFormatInput = (
    ToolStrategy[Any]
    | ProviderStrategy[Any]
    | AutoStrategy[Any]
    | type[Any]
    | dict[str, Any]
    | None
)


@dataclass(frozen=True)
class StructuredOutputSpec:
    schema_sha256: str
    strategy: str
    synthetic_tool_names: tuple[str, ...]
    native: Any


def _schema(value):
    return copy_json_value(value) if type(value) is dict else value


def _handling(value):
    value = _original_formatter(value)
    if type(value) in (bool, str):
        return value
    if isinstance(value, type) and issubclass(value, Exception):
        return {"exception": qualified(value)}
    if type(value) is tuple and all(
        isinstance(item, type) and issubclass(item, Exception) for item in value
    ):
        return {"exceptions": [qualified(item) for item in value]}
    if callable(value):
        return {
            "callable": value.__module__ + "." + value.__qualname__
            if type(getattr(value, "__qualname__", None)) is str
            else qualified(value),
            "revision_bound": True,
        }
    raise ValueError


def _original_formatter(value):
    if (
        type(value) is partial
        and value.func is _format_error
        and len(value.args) == 1
        and not value.keywords
    ):
        return value.args[0]
    return value


def _format_error(callback, error):
    failed = False
    result = None
    try:
        result = callback(error)
        if type(result) is not str:
            raise ValueError
    except Exception:
        failed = True
    if failed:
        raise ConfigurationError("structured_output_validation_failed")
    return result


def inspect_response_format(value: ResponseFormatInput) -> StructuredOutputSpec | None:
    if value is None:
        return None
    failed = False
    result = None
    try:
        if type(value) is ToolStrategy:
            handling = _original_formatter(value.handle_errors)
            if callable(handling) and not (
                isinstance(handling, type) and issubclass(handling, Exception)
            ):
                handling = partial(_format_error, handling)
            native = ToolStrategy(
                _schema(value.schema),
                tool_message_content=value.tool_message_content,
                handle_errors=handling,
            )
            strategy, specs = "tool", native.schema_specs
            options = {
                "tool_message_content": native.tool_message_content,
                "handle_errors": _handling(native.handle_errors),
            }
        elif type(value) is ProviderStrategy:
            native = ProviderStrategy(
                _schema(value.schema), strict=value.schema_spec.strict
            )
            strategy, specs, options = "provider", [native.schema_spec], {}
        elif type(value) is AutoStrategy:
            native = AutoStrategy(_schema(value.schema))
            strategy, specs, options = (
                "auto",
                ToolStrategy(native.schema).schema_specs,
                {},
            )
        else:
            native = _schema(value)
            strategy, specs, options = "auto", ToolStrategy(native).schema_specs, {}
        schema = [
            {
                "schema": item.json_schema,
                "name": item.name
                if not (type(item.schema) is dict and not item.schema.get("title"))
                else None,
                "description": item.description,
                "kind": item.schema_kind,
                "strict": item.strict,
            }
            for item in specs
        ]
        names = tuple(
            item.name
            for item in specs
            if not (type(item.schema) is dict and not item.schema.get("title"))
        )
        result = StructuredOutputSpec(
            payload_hash({"strategy": strategy, "schemas": schema, **options}),
            strategy,
            names,
            native,
        )
    except Exception:
        failed = True
    if failed:
        raise ConfigurationError("invalid_integration_configuration")
    return result


def _native_specs(spec):
    value = spec.native
    if type(value) is ToolStrategy:
        return value.schema_specs
    if type(value) is ProviderStrategy:
        return [value.schema_spec]
    return ToolStrategy(
        value.schema if type(value) is AutoStrategy else value
    ).schema_specs


def validate_durable_response_format(spec):
    if spec is None:
        return
    failed = False
    try:
        for item in _native_specs(spec):
            if type(item.schema) is dict and (
                type(item.schema.get("title")) is not str or not item.schema["title"]
            ):
                raise ValueError
            if item.schema_kind != "json_schema":
                core = TypeAdapter(item.schema).core_schema

                def serializers(value):
                    if type(value) is dict:
                        if value.get("serialization") is not None or value.get(
                            "computed_fields"
                        ):
                            raise ValueError
                        for child in value.values():
                            serializers(child)
                    elif type(value) in (list, tuple):
                        for child in value:
                            serializers(child)

                serializers(core)
                if (
                    isinstance(item.schema, type)
                    and item.schema.__getattribute__ is not object.__getattribute__
                ):
                    raise ValueError
                if (
                    isinstance(item.schema, type)
                    and issubclass(item.schema, BaseModel)
                    and item.schema.model_dump is not BaseModel.model_dump
                ):
                    raise ValueError
        for schema in _projection_schemas(spec):
            schema = copy_json_value(schema)
            _projection_validator(schema).check_schema(schema)

            def refs(value):
                if type(value) is dict:
                    for key, child in value.items():
                        if key in {"$ref", "$dynamicRef"} and (
                            type(child) is not str or not child.startswith("#")
                        ):
                            raise ValueError
                        refs(child)
                elif type(value) is list:
                    for child in value:
                        refs(child)

            refs(schema)
    except Exception:
        failed = True
    if failed:
        raise ConfigurationError("unsupported_execution_method")


def _projection_schemas(spec):
    value = spec.native
    schema = (
        value.schema
        if type(value) in {ToolStrategy, ProviderStrategy, AutoStrategy}
        else value
    )
    if type(schema) is dict:
        return [schema]
    return [item.json_schema for item in _native_specs(spec)]


def _projection_validator(schema):
    from jsonschema import Draft202012Validator
    from jsonschema.validators import validator_for

    if "$schema" not in schema:
        return Draft202012Validator
    validator = validator_for(schema, default=None)
    if validator is None:
        raise ValueError
    return validator


def validate_response_bindings(build, spec):
    if spec is None:
        return
    fields = getclosurevars(build.graph.nodes["model"].bound.func).nonlocals
    execute = getclosurevars(fields["_execute_model_sync"]).nonlocals
    configuration = getclosurevars(execute["_get_bound_model"]).nonlocals
    synthetic = set(configuration["structured_output_tools"])
    executable = {name for _, name in build.bindings.native}
    if synthetic & executable or set(spec.synthetic_tool_names) & executable:
        raise ConfigurationError("conflicting_tool_definitions")


def response_configuration(graph):
    """Measure the actual native parser/bindings, including post-construction changes."""
    fields = getclosurevars(graph.nodes["model"].bound.func).nonlocals
    initial = fields["initial_response_format"]
    if initial is None:
        return None
    execute = getclosurevars(fields["_execute_model_sync"]).nonlocals
    configuration = getclosurevars(execute["_get_bound_model"]).nonlocals
    output = getclosurevars(execute["_handle_model_output"]).nonlocals
    bindings = configuration["structured_output_tools"]
    if output["structured_output_tools"] is not bindings:
        raise ConfigurationError("unsupported_graph_shape")
    setup = configuration["tool_strategy_for_setup"]
    specs = setup.schema_specs if setup is not None else []
    projected = {
        "initial": inspect_response_format(initial).schema_sha256,
        "setup": [
            {
                "name": item.name,
                "description": item.description,
                "kind": item.schema_kind,
                "schema": item.json_schema,
                "strict": item.strict,
            }
            for item in specs
        ],
        "bindings": [
            {
                "name": name,
                "kind": binding.schema_kind,
                "description": binding.tool.description,
                "schema": binding.tool.args_schema,
            }
            for name, binding in sorted(bindings.items())
        ],
    }
    if type(initial) is ProviderStrategy:
        item = initial.schema_spec
        projected["provider"] = {
            "name": item.name,
            "description": item.description,
            "kind": item.schema_kind,
            "schema": item.json_schema,
            "strict": item.strict,
        }
    handling = initial.handle_errors if type(initial) is ToolStrategy else None
    return payload_hash(projected), id(handling) if callable(handling) else None


async def invoke_native(operation):
    failed = False
    try:
        return await operation()
    except StructuredOutputError:
        failed = True
    except ConfigurationError as error:
        if error.reason != "structured_output_validation_failed":
            raise
        failed = True
    if failed:
        raise ConfigurationError("structured_output_validation_failed")


async def stream_native(iterator):
    failed = False
    try:
        async for item in iterator:
            yield item
    except StructuredOutputError:
        failed = True
    except ConfigurationError as error:
        if error.reason != "structured_output_validation_failed":
            raise
        failed = True
    finally:
        await iterator.aclose()
    if failed:
        raise ConfigurationError("structured_output_validation_failed")


def project_structured_response(spec, value):
    failed = False
    projected = None
    try:
        if isinstance(value, BaseModel):
            candidate = value.model_dump(mode="json")
        elif is_dataclass(value) and not isinstance(value, type):
            candidate = TypeAdapter(type(value)).dump_python(
                value, mode="json", warnings="error"
            )
        else:
            candidate = value
        candidate = json.loads(
            snapshot_json(candidate, "structured_output_projection_failed")
        )
        for schema in _projection_schemas(spec):
            if _projection_validator(schema)(schema).is_valid(candidate):
                projected = candidate
                break
        else:
            raise ValueError
    except Exception:
        failed = True
    if failed:
        raise ConfigurationError("structured_output_projection_failed")
    return projected
