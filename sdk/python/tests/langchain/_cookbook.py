"""Locate explicitly supplied cookbook applications for SDK qualification."""

import os
from pathlib import Path


def recipe(relative):
    value = os.environ.get("OPENEIGHT_COOKBOOK_ROOT")
    if not value:
        raise RuntimeError("Set OPENEIGHT_COOKBOOK_ROOT to a cookbook checkout")
    path = Path(value).expanduser().resolve() / relative
    if not path.is_dir():
        raise RuntimeError(f"Missing cookbook recipe: {path}")
    return path
