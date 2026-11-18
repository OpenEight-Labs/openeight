"""Inspect qualified native selectors and resolve enrolled configurations once."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from typing import Any, Mapping

from langchain.chat_models.base import _ConfigurableModel
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable, RunnableBinding
from langchain_core.runnables.config import var_child_runnable_config
from langchain_core.runnables.configurable import (
    RunnableConfigurableAlternatives,
    RunnableConfigurableFields,
)
from langchain_core.runnables.utils import (
    ConfigurableField,
    ConfigurableFieldMultiOption,
    ConfigurableFieldSingleOption,
)

from openeight._json import copy_json_value
from openeight.errors import ConfigurationError

from ._measurement import payload_hash, qualified

ModelInput = str | BaseChatModel | Runnable[Any, Any]
CHECKPOINT_KEYS = frozenset(
    {"thread_id", "checkpoint_id", "checkpoint_ns", "checkpoint_map"}
)
_PREPARING = ContextVar("openeight_preparing_models", default=False)


@contextmanager
def preparing_models():
    token = _PREPARING.set(True)
    try:
        yield
    finally:
        _PREPARING.reset(token)


def snapshot_model(value, *, enrolled_keys=(), deep=False, ancestors=()):
    capture_model(value, enrolled_keys=enrolled_keys, deep=deep, ancestors=ancestors)
    if isinstance(value, (str, BaseChatModel)):
        return value
    ancestors = (*ancestors, id(value))
    if type(value) is _ConfigurableModel:
        return _ConfigurableModel(
            default_config=_defaults(value._default_config),
            configurable_fields=value._configurable_fields
            if value._configurable_fields == "any"
            else tuple(value._configurable_fields),
            config_prefix=value._config_prefix,
            queued_declarative_operations=[
                (name, tuple(args), _defaults(kwargs))
                for name, args, kwargs in value._queued_declarative_operations
            ],
        )
    if type(value) is RunnableConfigurableFields:
        fields = {
            key: spec._replace(
                options={
                    name: _selection(option) for name, option in spec.options.items()
                },
                default=_selection(spec.default),
            )
            if type(spec)
            in {ConfigurableFieldSingleOption, ConfigurableFieldMultiOption}
            else spec
            for key, spec in value.fields.items()
        }
        return RunnableConfigurableFields(
            default=value.default,
            fields=fields,
            config=_defaults(value.config) if value.config else None,
        )
    if type(value) is RunnableConfigurableAlternatives:
        return RunnableConfigurableAlternatives(
            default=snapshot_model(
                value.default,
                enrolled_keys=enrolled_keys,
                deep=deep,
                ancestors=ancestors,
            ),
            alternatives={
                name: snapshot_model(
                    model, enrolled_keys=enrolled_keys, deep=deep, ancestors=ancestors
                )
                for name, model in value.alternatives.items()
            },
            which=value.which,
            default_key=value.default_key,
            prefix_keys=value.prefix_keys,
            config=_defaults(value.config) if value.config else None,
        )
    if type(value) is RunnableBinding:
        return RunnableBinding(
            bound=snapshot_model(
                value.bound, enrolled_keys=enrolled_keys, deep=deep, ancestors=ancestors
            ),
            kwargs={},
            config=_defaults(value.config),
        )
    invalid()


@dataclass(frozen=True)
class ResolvedInvocationModel:
    variant_name: str
    model: BaseChatModel
    profile: object | None
    configuration_sha256: str


def invalid():
    raise ConfigurationError("invalid_integration_configuration")


def _sensitive(key):
    key = key.lower().replace("-", "_")
    return (
        key.startswith(("__", "openeight"))
        or key in CHECKPOINT_KEYS
        or key == "token"
        or key.endswith("_token")
        or any(
            part in key
            for part in (
                "credential",
                "api_key",
                "apikey",
                "secret",
                "client",
                "authorization",
            )
        )
    )


def _defaults(value, *, measured=False):
    if isinstance(value, Mapping):
        result = {}
        for key, child in value.items():
            if type(key) is not str:
                invalid()
            if _sensitive(key):
                result[key] = {"application_owned": True} if measured else child
            else:
                result[key] = _defaults(child, measured=measured)
        return result
    if type(value) in (list, tuple):
        return [_defaults(child, measured=measured) for child in value]
    failed = False
    copied = None
    try:
        copied = copy_json_value(value)
    except Exception:
        failed = True
    if failed:
        invalid()
    return copied


def _selection(value):
    if isinstance(value, Mapping):
        if any(type(key) is not str or _sensitive(key) for key in value):
            invalid()
        return {key: _selection(child) for key, child in value.items()}
    if type(value) in (list, tuple):
        return [_selection(child) for child in value]
    failed = False
    result = None
    try:
        result = copy_json_value(value)
    except Exception:
        failed = True
    if failed:
        invalid()
    return result


@contextmanager
def _native_context():
    token = var_child_runnable_config.set(None)
    try:
        yield
    finally:
        var_child_runnable_config.reset(token)


def model_is_configurable(value):
    if type(value) in {
        _ConfigurableModel,
        RunnableConfigurableFields,
        RunnableConfigurableAlternatives,
    }:
        return True
    if type(value) is RunnableBinding:
        return model_is_configurable(value.bound)
    return False


def _bound_selectors(config, keys, metadata_values=None):
    if not config:
        return {}
    if type(config) is not dict or set(config) - {
        "configurable",
        "tags",
        "metadata",
        "callbacks",
        "recursion_limit",
    }:
        invalid()
    selectors = config.get("configurable", {})
    if (
        type(selectors) is not dict
        or config.get("tags")
        or config.get("callbacks")
        or config.get("recursion_limit", 25) != 25
    ):
        invalid()
    metadata = config.get("metadata", {})
    if type(metadata) is not dict or set(metadata) - set(keys):
        invalid()
    declared = {**(metadata_values or {}), **selectors}
    if any(
        key not in declared or payload_hash(item) != payload_hash(declared[key])
        for key, item in metadata.items()
    ):
        invalid()
    return _selection(selectors)


def capture_model(value, *, enrolled_keys=(), deep=False, ancestors=()):
    failed = False
    result = None
    try:
        result = _capture_model(
            value, enrolled_keys=enrolled_keys, deep=deep, ancestors=ancestors
        )
    except Exception:
        failed = True
    if failed:
        invalid()
    return result


def _model_field(model, key):
    values = object.__getattribute__(model, "__dict__")
    if key not in values:
        invalid()
    return values[key]


def _capture_model(value, *, enrolled_keys=(), deep=False, ancestors=()):
    """Return pure normalization/resolution callbacks over one captured declaration."""
    if id(value) in ancestors or len(ancestors) >= 16:
        invalid()
    ancestors = (*ancestors, id(value))
    if isinstance(value, BaseChatModel) or isinstance(value, str):
        if isinstance(value, str) and not value.strip():
            invalid()
        projection = {
            "kind": "static",
            "class": qualified(value) if not isinstance(value, str) else None,
            "model_spec_sha256": payload_hash(value)
            if isinstance(value, str)
            else None,
        }

        def normalize(selectors):
            return projection

        def resolve(selectors):
            if isinstance(value, BaseChatModel):
                return value, None
            from langchain.chat_models import init_chat_model

            if deep:
                from deepagents._models import resolve_model

                return resolve_model(value), value
            return init_chat_model(value), value

        return normalize, resolve, frozenset()

    if type(value) is _ConfigurableModel:
        defaults = _defaults(value._default_config)
        fields = value._configurable_fields
        if fields != "any" and (
            type(fields) not in (tuple, list)
            or any(type(field) is not str or _sensitive(field) for field in fields)
        ):
            invalid()
        fields = "any" if fields == "any" else tuple(fields)
        prefix = value._config_prefix
        if type(prefix) is not str:
            invalid()
        names = (
            {key[len(prefix) :] for key in enrolled_keys if key.startswith(prefix)}
            if fields == "any"
            else set(fields)
        )
        keys = frozenset(prefix + key for key in names)
        if any(_sensitive(key) for key in names):
            invalid()
        native = _ConfigurableModel(
            default_config=defaults, configurable_fields=fields, config_prefix=prefix
        )
        for operation, args, kwargs in tuple(value._queued_declarative_operations):
            if operation != "with_config" or args or set(kwargs) != {"config"}:
                invalid()
            remaining = _bound_selectors(
                kwargs["config"],
                keys | {prefix + key for key in defaults},
                {prefix + key: item for key, item in defaults.items()},
            )
            if remaining:
                invalid()
        measured_defaults = _defaults(defaults, measured=True)

        def parameters(selectors):
            with _native_context():
                return {
                    **defaults,
                    **native._model_params({"configurable": dict(selectors)}),
                }

        metadata, configurations = {}, {}

        def configuration(selectors):
            from langchain.chat_models.base import _get_chat_model_creator, _parse_model

            if deep:
                from openeight._framework._diagnostics import (
                    safe_resolution_diagnostics,
                )

                safe_resolution_diagnostics()

            selected = parameters(selectors)
            model = selected.get("model")
            declared_provider = selected.get("model_provider")
            if type(model) is not str or not model.strip():
                invalid()
            name, provider = _parse_model(model, declared_provider)
            original = (
                model if declared_provider is None else declared_provider + ":" + name
            )
            if provider not in metadata:
                if metadata and not _PREPARING.get():
                    invalid()
                creator = _get_chat_model_creator(provider)
                if type(creator) is not partial or "cls" not in creator.keywords:
                    invalid()
                model_class = creator.keywords["cls"]
                if not isinstance(model_class, type) or not issubclass(
                    model_class, BaseChatModel
                ):
                    invalid()
                available = dict(model_class.model_fields)
                available.update(
                    {
                        field.alias: field
                        for field in model_class.model_fields.values()
                        if type(field.alias) is str
                    }
                )
                native_defaults = {}
                for key in names - {"model", "model_provider"}:
                    field = available.get(key)
                    if field is None:
                        invalid()
                    if field.default_factory is not None or field.is_required():
                        if key not in selected:
                            invalid()
                        native_defaults[key] = {"explicit_required": True}
                    else:
                        native_defaults[key] = _defaults(field.default, measured=True)
                metadata[provider] = available, native_defaults
            available, native_defaults = metadata[provider]
            params = {
                key: item
                for key, item in selected.items()
                if key not in {"model", "model_provider"}
            }
            if set(params) - set(available):
                invalid()
            base = {
                "kind": "init-chat-model",
                "fields": fields if fields == "any" else list(fields),
                "prefix": prefix,
                "defaults": measured_defaults,
                "native_defaults": native_defaults,
                "provided_fields": sorted(selected),
                "effective": _defaults(selected, measured=True),
            }
            signature = payload_hash(base)
            if signature not in configurations:
                if (
                    configurations
                    and not _PREPARING.get()
                    or deep
                    and not _PREPARING.get()
                ):
                    invalid()
                if deep:
                    from deepagents.profiles.provider.provider_profiles import (
                        apply_provider_profile,
                    )

                    from openeight._framework._diagnostics import (
                        safe_resolution_diagnostics,
                    )

                    safe_resolution_diagnostics()
                    params = {**apply_provider_profile(original), **params}
                if set(params) - set(available):
                    invalid()
                actual = _defaults(params)
                projected = {
                    **base,
                    "effective_provider_parameters": _defaults(actual, measured=True),
                }
                configurations[signature] = projected, name, provider, actual, original
            return configurations[signature]

        def normalize(selectors):
            return configuration(selectors)[0]

        def resolve(selectors):
            from langchain.chat_models import init_chat_model

            _, name, provider, params, original = configuration(selectors)
            return init_chat_model(name, model_provider=provider, **params), original

        return normalize, resolve, keys

    if type(value) is RunnableConfigurableFields:
        if not isinstance(value.default, BaseChatModel):
            invalid()
        defaults = value.default
        specs = dict(value.fields)
        known = type(defaults).model_fields
        if any(key not in known or _sensitive(key) for key in specs):
            invalid()
        for spec in specs.values():
            if (
                type(spec)
                not in {
                    ConfigurableField,
                    ConfigurableFieldSingleOption,
                    ConfigurableFieldMultiOption,
                }
                or type(spec.id) is not str
                or _sensitive(spec.id)
            ):
                invalid()
            if type(spec) in {
                ConfigurableFieldSingleOption,
                ConfigurableFieldMultiOption,
            } and any(type(name) is not str for name in spec.options):
                invalid()
        specs = {
            key: spec._replace(
                options={
                    name: _selection(option) for name, option in spec.options.items()
                },
                default=_selection(spec.default),
            )
            if type(spec)
            in {ConfigurableFieldSingleOption, ConfigurableFieldMultiOption}
            else spec
            for key, spec in specs.items()
        }
        keys = frozenset(spec.id for spec in specs.values())
        if len(keys) != len(specs):
            invalid()
        bound = _bound_selectors(value.config, keys)
        original_defaults = {
            key: _selection(_model_field(defaults, key)) for key in specs
        }
        native = RunnableConfigurableFields(default=defaults, fields=specs)

        def effective(selectors):
            merged = {**bound, **selectors}
            result, choices = {}, {}
            for key, spec in specs.items():
                if type(spec) is ConfigurableField:
                    result[key] = merged.get(spec.id, original_defaults[key])
                elif type(spec) is ConfigurableFieldSingleOption:
                    choice = merged.get(spec.id) or spec.default
                    result[key], choices[spec.id] = spec.options[choice], choice
                else:
                    choices[spec.id] = merged.get(spec.id, spec.default)
                    result[key] = [spec.options[choice] for choice in choices[spec.id]]
            return _selection(result), _selection(choices)

        def normalize(selectors):
            result, choices = effective(selectors)
            return {
                "kind": "fields",
                "class": qualified(defaults),
                "ids": {key: spec.id for key, spec in specs.items()},
                "defaults": original_defaults,
                "bound": bound,
                "effective": result,
                "choices": choices,
            }

        def resolve(selectors):
            if any(
                payload_hash(_model_field(defaults, key))
                != payload_hash(original_defaults[key])
                for key in specs
            ):
                invalid()
            with _native_context():
                resolved, _ = native._prepare({"configurable": {**bound, **selectors}})
            if not isinstance(resolved, BaseChatModel):
                invalid()
            return resolved, None

        return normalize, resolve, keys

    if type(value) is RunnableConfigurableAlternatives:
        if (
            type(value.which) is not ConfigurableField
            or _sensitive(value.which.id)
            or type(value.prefix_keys) is not bool
        ):
            invalid()
        which_id, default_key, prefix_keys = (
            value.which.id,
            value.default_key,
            value.prefix_keys,
        )
        alternatives = {default_key: value.default, **value.alternatives}
        if default_key in value.alternatives or any(
            type(key) is not str or not key for key in alternatives
        ):
            invalid()
        entries, keys = {}, {which_id}
        for key, child in alternatives.items():
            child_prefix = which_id + "==" + key + "/" if prefix_keys else ""
            child_enrolled = [
                field.removeprefix(child_prefix) for field in enrolled_keys
            ]
            entry = capture_model(
                child, enrolled_keys=child_enrolled, deep=deep, ancestors=ancestors
            )
            entries[key] = entry
            keys.update(child_prefix + field for field in entry[2])
        keys = frozenset(keys)
        bound = _bound_selectors(value.config, keys)
        native = RunnableConfigurableAlternatives(
            default=value.default,
            which=value.which,
            alternatives=dict(value.alternatives),
            default_key=default_key,
            prefix_keys=prefix_keys,
        )

        def selected(selectors):
            merged = {**bound, **selectors}
            key = merged.get(which_id, default_key)
            if key not in entries:
                invalid()
            with _native_context():
                _, config = native._prepare({"configurable": merged})
            return key, config["configurable"]

        def normalize(selectors):
            key, remapped = selected(selectors)
            return {
                "kind": "alternatives",
                "field": which_id,
                "key": key,
                "default": default_key,
                "prefix_keys": prefix_keys,
                "bound": bound,
                "selected": entries[key][0](remapped),
            }

        def resolve(selectors):
            key, remapped = selected(selectors)
            return entries[key][1](remapped)

        return normalize, resolve, keys

    if type(value) is RunnableBinding:
        if (
            value.kwargs
            or value.config_factories
            or not model_is_configurable(value.bound)
        ):
            invalid()
        normalize_child, resolve_child, keys = capture_model(
            value.bound, enrolled_keys=enrolled_keys, deep=deep, ancestors=ancestors
        )
        bound = _bound_selectors(value.config, keys)

        def normalize(selectors):
            return {
                "kind": "binding",
                "bound": bound,
                "model": normalize_child({**bound, **selectors}),
            }

        def resolve(selectors):
            return resolve_child({**bound, **selectors})

        return normalize, resolve, keys
    invalid()


def selector_values(config, allowed):
    selectors = config.get("configurable", {})
    if (
        not isinstance(selectors, Mapping)
        or set(selectors) - set(allowed) - CHECKPOINT_KEYS
    ):
        invalid()
    return _selection(
        {key: item for key, item in selectors.items() if key not in CHECKPOINT_KEYS}
    )


def resolve_captured(callback, selectors):
    failed = False
    result = None
    try:
        result = callback(selectors)
    except Exception:
        failed = True
    if failed or result is None or not isinstance(result[0], BaseChatModel):
        invalid()
    return result


def normalize_captured(callback, selectors):
    failed = False
    result = None
    try:
        result = callback(selectors)
        payload_hash(result)
    except Exception:
        failed = True
    if failed or result is None:
        invalid()
    return result
