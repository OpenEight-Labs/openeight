"""Construction-time authority capture and immutable managed configuration."""

import subprocess
import sys
from pathlib import Path

import pytest

from openeight import (
    ConfigurationError,
    ModelVariant,
    OpenEightConfig,
    RuntimeModelConfig,
)
from openeight.configuration import normalize_config

ENV = {
    "OPENEIGHT_ENDPOINT": "https://control.example",
    "OPENEIGHT_API_KEY": "ctk_environment",
}


def test_config_forms_snapshot_authority_and_redact_secrets():
    supplied = {"agent_name": "support"}
    normalized = normalize_config(supplied, environ=ENV, observed=False)
    other = normalize_config(OpenEightConfig(**supplied), environ=ENV, observed=False)
    assert normalized == other
    assert normalized.governance.credential == "ctk_environment"
    assert "ctk_environment" not in repr(normalized)
    supplied["credential"] = "ctk_changed"
    assert normalized.governance.credential == "ctk_environment"


@pytest.mark.parametrize(
    "field,value",
    [
        ("endpoint", ""),
        ("credential", ""),
        ("credential", "secret"),
        ("endpoint", "http://external.example"),
    ],
)
def test_invalid_explicit_values_never_fall_back(field, value):
    with pytest.raises(ConfigurationError) as caught:
        normalize_config(
            {"agent_name": "support", field: value}, environ=ENV, observed=False
        )
    assert value not in repr(caught.value) if value else True


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"agent_name": "support", "typo": True},
        {"agent_name": "support", "runtime_models": object()},
    ],
)
def test_invalid_configuration_rejects(value):
    with pytest.raises(ConfigurationError):
        normalize_config(value, environ=ENV, observed=False)


@pytest.mark.parametrize(
    "settings", [{"on_block": "raise"}, {"approval": object()}, {"durable": object()}]
)
def test_observation_rejects_governed_settings(settings):
    with pytest.raises(ConfigurationError):
        normalize_config(
            {"agent_name": "support", **settings}, environ=ENV, observed=True
        )


def test_variant_containers_are_defensively_frozen():
    values = {"temperature": 0.5, "options": {"stop": ["end"]}}
    variant = ModelVariant(name="primary", configurable=values)
    variants = [variant]
    configuration = RuntimeModelConfig(variants=variants)
    values["options"]["stop"].append("changed")
    variants.clear()
    assert variant.configurable["options"]["stop"] == ("end",)
    assert configuration.variants == (variant,)
    with pytest.raises(TypeError):
        variant.configurable["temperature"] = 1


def test_root_import_does_not_load_optional_frameworks():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, openeight; assert not any(k.split('.')[0] in {'langchain', 'deepagents'} for k in sys.modules)",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
