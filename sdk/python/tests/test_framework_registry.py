"""The registry narrows support and never infers untested feature combinations."""

import copy
import json
from pathlib import Path

import pytest

from openeight import ConfigurationError
from openeight._framework import InstalledFramework, select_compatibility
from openeight._framework._registry import load_registry, validate_registry


def test_current_qualification_environment_matches_declared_support():
    root = Path(__file__).parents[1]
    inventory = json.loads((root / "constraints/managed-integrations.json").read_text())
    rows = {row["id"]: row for row in load_registry()["entries"]}
    for lane in inventory["lanes"]:
        lock = dict(
            line.split("==")
            for line in (root / "constraints" / lane["lock"]).read_text().splitlines()
            if "==" in line
        )
        for identifier in lane["qualification_ids"]:
            row = rows[identifier]
            assert all(lock.get(k) == v for k, v in row["packages"].items())
            assert set(inventory["python"]) <= set(row["python"])


@pytest.mark.parametrize("row", load_registry()["entries"], ids=lambda row: row["id"])
def test_all_declared_combinations_select_exactly_their_row(row):
    for python in row["python"]:
        for features in row["feature_sets"]:
            installed = InstalledFramework(
                row["integration"], python, {**row["packages"], "unrelated": "99.0.0"}
            )
            result = select_compatibility(installed, required=frozenset(features))
            assert result.qualification_id == row["id"]
            assert result.supported_features == frozenset(features)
            with pytest.raises(TypeError):
                result.installed.packages["langchain"] = "99.0.0"


def test_ordinary_does_not_require_optional_sqlite_or_grant_durable():
    row = next(
        row for row in load_registry()["entries"] if row["integration"] == "deepagents"
    )
    installed = InstalledFramework("deepagents", "3.12", row["packages"])
    assert select_compatibility(installed, required=frozenset({"ordinary"}))
    with pytest.raises(ConfigurationError) as error:
        select_compatibility(installed, required=frozenset({"durable"}))
    assert error.value.reason == "durable_dependencies_missing"


@pytest.mark.parametrize("change", ["package", "python", "integration"])
def test_unknown_runtime_rejects(change):
    row = load_registry()["entries"][0]
    values = dict(
        integration=row["integration"], python="3.12", packages=dict(row["packages"])
    )
    if change == "package":
        values["packages"]["langchain"] = "1.4.4"
    else:
        values[change] = "unknown"
    with pytest.raises(ConfigurationError) as error:
        select_compatibility(
            InstalledFramework(**values), required=frozenset({"ordinary"})
        )
    assert error.value.reason == "unsupported_framework_version"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.update(format_version=True),
        lambda data: data["entries"][0].update(extra=True),
        lambda data: data["entries"].append(copy.deepcopy(data["entries"][0])),
        lambda data: data["entries"][0].update(implementation_id="external.plugin"),
        lambda data: data["entries"][0].update(implementation_revision=True),
        lambda data: data["entries"][0].update(checkpoint_format=None),
        lambda data: data["entries"][0]["packages"].update(langchain="latest"),
        lambda data: data["entries"][0].update(feature_sets=[["ordinary", "ordinary"]]),
        lambda data: data["entries"][0].update(feature_sets=[["durable"]]),
        lambda data: data["entries"][0].update(qualification_cases=["private"]),
    ],
)
def test_malformed_registry_fails_closed(mutate):
    value = copy.deepcopy(load_registry())
    mutate(value)
    with pytest.raises(ValueError):
        validate_registry(value)


def test_optional_package_rows_cannot_create_ambiguous_matching():
    value = load_registry()
    extra = copy.deepcopy(value["entries"][0])
    extra["id"] = "ambiguous"
    extra["packages"]["aiosqlite"] = "0.22.1"
    value["entries"].append(extra)
    with pytest.raises(ValueError, match="ambiguous"):
        validate_registry(value)


@pytest.mark.parametrize("flavor", ["pypy", "prerelease", "free_threaded"])
def test_unsupported_interpreter_rejects_before_package_lookup(monkeypatch, flavor):
    from types import SimpleNamespace

    from openeight._framework import _registry

    if flavor == "pypy":
        monkeypatch.setattr(
            _registry.sys, "implementation", SimpleNamespace(name="pypy")
        )
    elif flavor == "prerelease":
        monkeypatch.setattr(
            _registry.sys, "version_info", SimpleNamespace(releaselevel="candidate")
        )
    else:
        monkeypatch.setattr(_registry.sysconfig, "get_config_var", lambda key: 1)
    with pytest.raises(ConfigurationError) as error:
        _registry.detect_framework(
            "deepagents", version=lambda name: pytest.fail("package lookup")
        )
    assert error.value.reason == "unsupported_framework_version"


@pytest.mark.parametrize("missing", ["aiosqlite", "langgraph-checkpoint-sqlite"])
def test_missing_durable_packages_have_distinct_failure(missing):
    row = next(
        r for r in load_registry()["entries"] if r["id"] == "langchain-durable-01"
    )
    packages = dict(row["packages"])
    del packages[missing]
    with pytest.raises(ConfigurationError) as error:
        select_compatibility(
            InstalledFramework("langchain", "3.12", packages),
            required=frozenset({"durable"}),
        )
    assert error.value.reason == "durable_dependencies_missing"


@pytest.mark.parametrize("sqlite_version", ["3.1.0", "3.1.1", "3.1.2"])
def test_deep_durable_selection_requires_a_declared_exact_optional_tuple(
    sqlite_version,
):
    registry = load_registry()
    row = next(r for r in registry["entries"] if r["id"] == "deepagents-01")
    packages = {
        **row["packages"],
        "aiosqlite": "0.22.1",
        "langgraph-checkpoint-sqlite": sqlite_version,
    }
    declared = any(r["id"] == "deepagents-durable-01" for r in registry["entries"])
    if declared and sqlite_version == "3.1.1":
        result = select_compatibility(
            InstalledFramework("deepagents", "3.12", packages),
            required=frozenset({"durable", "local_durable", "sync"}),
        )
        assert result.qualification_id == "deepagents-durable-01"
        assert result.checkpoint_format == "deepagents-sqlite-v1"
    else:
        with pytest.raises(ConfigurationError) as error:
            select_compatibility(
                InstalledFramework("deepagents", "3.12", packages),
                required=frozenset({"durable"}),
            )
        assert error.value.reason == "unsupported_framework_feature"
