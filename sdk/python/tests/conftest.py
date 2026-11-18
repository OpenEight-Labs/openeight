"""Optional application qualification consumes an explicit cookbook checkout."""

import os
from pathlib import Path

import pytest


def pytest_configure(config):
    value = os.environ.get("OPENEIGHT_COOKBOOK_ROOT")
    if value:
        root = Path(value).expanduser().resolve()
        for relative in (
            "agents/customer-support/cli.py",
            "patterns/checkpoint-recovery/application.py",
        ):
            if not (root / relative).is_file():
                raise pytest.UsageError(
                    "OPENEIGHT_COOKBOOK_ROOT must contain the cookbook recipes"
                )
