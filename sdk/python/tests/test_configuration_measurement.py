"""Configuration fingerprints distinguish types and reject opaque state."""

import math

import pytest

from openeight import ConfigurationError
from openeight._framework._measurement import freeze, payload_hash, plain


def test_measurement_is_canonical_without_type_or_tag_collisions():
    assert payload_hash({"a": 1, "b": [2, 3]}) == payload_hash({"b": (2, 3), "a": 1})
    values = [None, False, 0, 0.0, -0.0, "0", ["int", "0"], {"int": "0"}]
    assert len({payload_hash(value) for value in values}) == len(values)
    assert payload_hash("é") != payload_hash("e\u0301")


@pytest.mark.parametrize(
    "value", [math.inf, -math.inf, math.nan, {1: "value"}, object(), "\ud800"]
)
def test_opaque_or_invalid_configuration_never_falls_back_to_repr(value):
    with pytest.raises(ConfigurationError) as caught:
        payload_hash(value)
    assert caught.value.reason == "unsupported_graph_shape"
    assert caught.value.__context__ is None


def test_measured_projection_is_immutable_and_roundtrips_to_json_containers():
    original = {"nodes": [{"path": [], "value": "captured"}]}
    captured = freeze(original)
    original["nodes"][0]["value"] = "mutated"
    assert plain(captured) == {"nodes": [{"path": [], "value": "captured"}]}
    with pytest.raises(TypeError):
        captured["nodes"][0]["value"] = "mutated"
