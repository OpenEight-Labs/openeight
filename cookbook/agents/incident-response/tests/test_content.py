from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from content import (
    SENTINEL,
    build_configuration,
    redact_synthetic_content,
)
from contracts import Settings


def test_redaction_copies_nested_values_and_is_concurrent():
    value = {
        "nested": [SENTINEL + " control", {"number": 2, "bool": True, "null": None}]
    }
    with ThreadPoolExecutor(8) as pool:
        results = list(
            pool.map(lambda _: redact_synthetic_content(None, value), range(20))
        )
    assert all(
        r
        == {"nested": ["[REDACTED] control", {"number": 2, "bool": True, "null": None}]}
        for r in results
    )
    assert value["nested"][0] == SENTINEL + " control"
    assert results[0] is not results[1]


def test_redactor_selection_never_enables_capture():
    settings = Settings(
        "http://localhost:7777",
        "incident-test",
        "ctk_test",
        redaction="synthetic-sentinels",
    )
    openeight = build_configuration(settings=settings)
    assert openeight.capture_content == "none"
    assert openeight.redact_content is redact_synthetic_content
    assert (
        build_configuration(
            settings=replace(settings, capture_content="all")
        ).capture_content
        == "all"
    )
    assert (
        build_configuration(settings=replace(settings, redaction="none")).redact_content
        is None
    )
