"""Catalog validation and drift detection do not require either SDK runtime."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from generate_error_catalog import ROOT, generate, render_outputs, unique_object, validate_catalog


class ErrorCatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = json.loads((ROOT / "contracts/errors.json").read_text())

    def test_checked_in_outputs_are_current(self):
        self.assertEqual(generate(ROOT, check=True), 12)

    def test_generation_is_order_independent(self):
        reordered = copy.deepcopy(self.catalog)
        reordered["errors"].reverse()
        self.assertEqual(render_outputs(self.catalog), render_outputs(reordered))

    def test_duplicate_reasons_are_rejected(self):
        self.catalog["errors"].append(self.catalog["errors"][0])
        with self.assertRaisesRegex(ValueError, "Duplicate public reason"):
            validate_catalog(self.catalog)

    def test_invalid_records_are_rejected(self):
        for field, value in [("domain", "private.domain"), ("http_status", 200), ("http_status", True), ("connect_code", "unknown"), ("reason", "bad reason"), ("message", "private server details")]:
            with self.subTest(field=field, value=value):
                candidate = copy.deepcopy(self.catalog)
                candidate["errors"][0][field] = value
                with self.assertRaises(ValueError):
                    validate_catalog(candidate)

    def test_duplicate_fields_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate catalog field"):
            json.loads('{"errors":[],"errors":[]}', object_pairs_hook=unique_object)

    def test_drift_check_does_not_rewrite_generated_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "contracts/errors.json"
            source.parent.mkdir()
            source.write_text(json.dumps(self.catalog))
            for name in render_outputs(self.catalog):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
            generate(root)
            path = root / "python/openeight/_error_catalog.py"
            path.write_text("stale\n")
            with self.assertRaisesRegex(ValueError, "stale"):
                generate(root, check=True)
            self.assertEqual(path.read_text(), "stale\n")


if __name__ == "__main__":
    unittest.main()
