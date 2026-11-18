"""Separate-process qualification of a nonterminal native reconciliation pause."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

if os.name != "posix":
    pytest.skip("Native recovery probes require POSIX", allow_module_level=True)

pytest.importorskip("deepagents", reason="Native probes require the deepagents extra")
pytest.importorskip(
    "langgraph.checkpoint.sqlite.aio",
    reason="Native probes require the deepagents-durable extra",
)


def test_uncertain_effect_pause_then_confirmed_result_does_not_repeat_handler(tmp_path):
    script = Path(__file__).with_name("reconciliation_probe.py")
    for phase in ("start", "resume"):
        result = subprocess.run(
            [sys.executable, str(script), str(tmp_path), phase],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
    first = json.loads((tmp_path / "start.json").read_text())
    final = json.loads((tmp_path / "resume.json").read_text())
    assert first["next"] and not first["errors"] and first["effects"] == 1
    assert not final["next"] and not final["errors"] and final["effects"] == 1
    assert final["messages"]
