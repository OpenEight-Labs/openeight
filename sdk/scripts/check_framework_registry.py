"""Validate the authoritative packaged framework support declaration."""

import json
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    schema = runpy.run_path(str(ROOT / "python/openeight/_framework/_schema.py"))
    source = ROOT / "python/openeight/_framework/frameworks.json"
    schema["validate_registry"](
        json.loads(source.read_text(), object_pairs_hook=schema["_unique_object"])
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
