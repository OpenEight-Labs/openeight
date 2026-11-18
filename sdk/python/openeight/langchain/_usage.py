"""Inclusive usage translation; unknown values remain absent."""

import re
from types import SimpleNamespace

_PROVIDERS = frozenset(
    {
        "openai",
        "anthropic",
        "azure_openai",
        "google_genai",
        "google_vertexai",
        "bedrock",
        "bedrock_converse",
        "ollama",
        "mistralai",
        "cohere",
        "groq",
    }
)
_MAX = (1 << 63) - 1


def normalize_usage(message, callback_metadata=None):
    metadata = getattr(message, "response_metadata", {})
    metadata = metadata if type(metadata) is dict else {}
    framework = callback_metadata if type(callback_metadata) is dict else {}
    provider = metadata.get("model_provider", framework.get("ls_provider"))
    model = metadata.get("model_name", framework.get("ls_model_name"))
    result, diagnostics = {}, []
    if type(provider) is str and provider in _PROVIDERS:
        result["provider"] = provider
        if (
            type(model) is str
            and "ctk_" not in model.lower()
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,255}", model)
        ):
            result["model"] = model
    standard = getattr(message, "usage_metadata", None)
    standard = standard if type(standard) is dict else {}
    details = standard.get("input_token_details", {})
    details = details if type(details) is dict else {}
    candidates = {
        "input_tokens": standard.get("input_tokens"),
        "output_tokens": standard.get("output_tokens"),
        "cache_read_input_tokens": details.get("cache_read"),
        "cache_creation_input_tokens": details.get("cache_creation"),
    }
    present = {
        "input_tokens": "input_tokens" in standard,
        "output_tokens": "output_tokens" in standard,
        "cache_read_input_tokens": "cache_read" in details,
        "cache_creation_input_tokens": "cache_creation" in details,
    }
    fallback = {}
    if type(provider) is str and provider in {"openai", "azure_openai"}:
        usage = metadata.get("token_usage", {})
        if type(usage) is dict:
            cached = usage.get("prompt_tokens_details", {})
            fallback = {
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
                "cache_read_input_tokens": cached.get("cached_tokens")
                if type(cached) is dict
                else None,
            }
    elif provider == "anthropic":
        usage = metadata.get("usage", {})
        if type(usage) is dict:
            raw = usage.get("input_tokens")
            subsets = [
                usage.get("cache_read_input_tokens", 0),
                usage.get("cache_creation_input_tokens", 0),
            ]
            inclusive = (
                raw + sum(subsets)
                if all(type(x) is int and 0 <= x <= _MAX for x in [raw, *subsets])
                else None
            )
            fallback = {
                "input_tokens": inclusive,
                "output_tokens": usage.get("output_tokens"),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
            }
    # Presence wins over fallback, even when a standard value is invalid. Otherwise
    # competing provider representations could double-count or hide bad usage.
    for key, value in candidates.items():
        if not present[key]:
            value = fallback.get(key)
        if value is None and not present[key]:
            continue
        if type(value) is int and 0 <= value <= _MAX:
            result[key] = value
        else:
            diagnostics.append("invalid_usage")
    subsets = result.get("cache_read_input_tokens", 0) + result.get(
        "cache_creation_input_tokens", 0
    )
    if "input_tokens" not in result or subsets > result["input_tokens"]:
        if (
            "cache_read_input_tokens" in result
            or "cache_creation_input_tokens" in result
        ):
            diagnostics.append("invalid_usage")
        result.pop("cache_read_input_tokens", None)
        result.pop("cache_creation_input_tokens", None)
    return result, tuple(set(diagnostics))


def normalize_result(response, callback_metadata=None):
    generations = getattr(response, "generations", ())
    try:
        messages = [
            getattr(generation, "message", None)
            for group in generations
            for generation in group
        ]
    except (TypeError, AttributeError):
        return {}, ("invalid_usage",)
    if len(messages) == 1 and messages[0] is not None:
        return normalize_usage(messages[0], callback_metadata)
    aggregate = getattr(response, "llm_output", None)
    if type(aggregate) is not dict:
        return {}, ("ambiguous_usage",) if messages else ()
    normalized, diagnostics = normalize_usage(
        SimpleNamespace(
            usage_metadata=aggregate.get("usage_metadata"), response_metadata=aggregate
        ),
        callback_metadata,
    )
    if not any(key in normalized for key in ("input_tokens", "output_tokens")):
        return normalized, (*diagnostics, "ambiguous_usage")
    return normalized, diagnostics
