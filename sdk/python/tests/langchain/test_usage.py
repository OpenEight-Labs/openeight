from types import SimpleNamespace
from openeight.langchain._usage import normalize_usage


def usage(standard=None, **metadata):
    return normalize_usage(
        SimpleNamespace(usage_metadata=standard, response_metadata=metadata)
    )[0]


def test_standard_inclusive_and_invalid_present_values():
    assert usage(
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "input_token_details": {"cache_read": 40},
            "output_token_details": {"reasoning": 5},
        }
    ) == {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 40}
    assert usage(
        {"input_tokens": True},
        model_provider="openai",
        token_usage={"prompt_tokens": 100},
    ) == {"provider": "openai"}
    assert "cache_read_input_tokens" not in usage(
        {"input_tokens": 5, "input_token_details": {"cache_read": 6}}
    )


def test_explicit_provider_fallback_accounting():
    assert usage(
        model_provider="anthropic",
        usage={"input_tokens": 60, "cache_read_input_tokens": 40, "output_tokens": 20},
    ) == {
        "provider": "anthropic",
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_input_tokens": 40,
    }
    assert usage(
        model_provider="openai",
        token_usage={
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 40},
        },
    ) == {
        "provider": "openai",
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_input_tokens": 40,
    }
    assert usage(token_usage={"prompt_tokens": 100}) == {}


def test_multiple_candidates_require_one_explicit_aggregate():
    from openeight.langchain._usage import normalize_result

    message = SimpleNamespace(
        usage_metadata={"input_tokens": 100, "output_tokens": 20}, response_metadata={}
    )
    result = SimpleNamespace(
        generations=[
            [SimpleNamespace(message=message), SimpleNamespace(message=message)]
        ],
        llm_output=None,
    )
    assert normalize_result(result) == ({}, ("ambiguous_usage",))
    result.llm_output = {
        "model_provider": "openai",
        "token_usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }
    assert normalize_result(result) == (
        {"provider": "openai", "input_tokens": 100, "output_tokens": 20},
        (),
    )


def test_malformed_identity_and_out_of_range_usage_are_safe():
    assert usage(model_provider=["openai"], token_usage={"prompt_tokens": 100}) == {}
    assert usage({"input_tokens": -1, "output_tokens": 2**63}) == {}
    assert usage(
        {
            "input_tokens": 100,
            "input_token_details": {"cache_read": 60, "cache_creation": 50},
        }
    ) == {"input_tokens": 100}
