"""The public distribution includes a lazy adapter without implicit providers."""

import shutil
import subprocess
import sys
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

from openeight import ConfigurationError, DeliveryConfig


@pytest.mark.parametrize(
    "values",
    [
        {"max_records": True},
        {"max_bytes": 0},
        {"flush_interval": float("nan")},
        {"late_callback_grace": float("inf")},
        {"max_records": 2, "flush_records": 3},
        {"max_bytes": 100, "flush_bytes": 101},
        {"flush_records": 1001},
    ],
)
def test_delivery_limits_reject_invalid_values(values):
    with pytest.raises(ConfigurationError):
        DeliveryConfig(**values)


def test_core_import_never_imports_framework():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            'import sys,openeight; print(any(n.startswith(("langchain", "langgraph", "deepagents", "mcp")) for n in sys.modules))',
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "False"


def test_distributions_exclude_tests_and_constraints(tmp_path):
    project = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    shutil.copytree(
        project,
        source,
        ignore=shutil.ignore_patterns(
            "build", "*.egg-info", "__pycache__", ".pytest_cache"
        ),
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(tmp_path),
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    (wheel,) = tmp_path.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert sum(n.endswith(".dist-info/METADATA") for n in names) == 1
        assert (
            archive.read("openeight/_framework/frameworks.json")
            == (project / "openeight/_framework/frameworks.json").read_bytes()
        )
        assert "openeight/client.py" in names
        assert "openeight/governance.py" in names
        license_name = "openeight-0.0.1.dist-info/licenses/LICENSE"
        assert archive.read(license_name) == (project / "LICENSE").read_bytes()
        metadata = archive.read("openeight-0.0.1.dist-info/METADATA").decode()
        assert "License-Expression: Apache-2.0" in metadata
        assert "License-File: LICENSE" in metadata
        assert "License-File: NOTICE" in metadata
        assert archive.read("openeight-0.0.1.dist-info/licenses/NOTICE") == (
            project / "NOTICE"
        ).read_bytes()
        assert "Requires-Dist: certifi>=2026.10.19" in metadata
        headers = Parser().parsestr(metadata)
        supported = SpecifierSet(headers["Requires-Python"])
        for version in ("3.11.0", "3.12.0", "3.13.0", "3.14.0", "3.15.0"):
            assert version in supported
        for version in ("3.10.9", "3.16.0"):
            assert version not in supported
        requirements = [
            Requirement(value) for value in headers.get_all("Requires-Dist", [])
        ]
        default_dependencies = {r.name for r in requirements if r.marker is None}
        from openeight.langchain._qualified import CLOSURES, PACKAGES

        framework_requirements = {
            r.name: r for r in requirements if r.marker is None and r.name in PACKAGES
        }
        # Fresh installations select the qualified managed tuple; historical
        # registry rows do not expand installation or managed lifecycle support.
        pins = ("1.4.3", "1.6.7", "1.2.14", "1.1.0", "4.3.0")
        for name, expected in zip(PACKAGES, pins, strict=True):
            assert str(framework_requirements[name].specifier) == "==" + expected
        assert pins in CLOSURES
        assert "1.7.4" not in framework_requirements["langchain-core"].specifier
        assert {
            "langchain",
            "langchain-core",
            "langgraph",
            "langgraph-prebuilt",
            "langgraph-checkpoint",
            "langgraph-sdk",
            "urllib3",
            "packaging",
        } <= default_dependencies
        assert not {"deepagents", "mcp"} & default_dependencies
        assert "langchain" in headers.get_all("Provides-Extra", [])
        assert "openeight/instrumentation.py" not in names
        assert {name.split("/", 1)[0] for name in names} == {
            "openeight",
            f"openeight-{headers['Version']}.dist-info",
        }
        assert not any(
            {"tests", "constraints", "compatibility", ".local"}.intersection(Path(n).parts) for n in names
        )
        for module in (project / "openeight").rglob("*.py"):
            assert (
                archive.read(module.relative_to(project).as_posix())
                == module.read_bytes()
            )
    (sdist,) = tmp_path.glob("*.tar.gz")
    with tarfile.open(sdist) as archive:
        assert not any(
            {"tests", "constraints", "compatibility", ".local"}.intersection(Path(member.name).parts[1:])
            for member in archive.getmembers()
        )
        for relative in [
            Path("LICENSE"),
            Path("NOTICE"),
            Path("CHANGELOG.md"),
            Path("openeight/_framework/frameworks.json"),
        ]:
            member = archive.extractfile(f"openeight-0.0.1/{relative.as_posix()}")
            assert member is not None
            assert member.read() == (project / relative).read_bytes()
