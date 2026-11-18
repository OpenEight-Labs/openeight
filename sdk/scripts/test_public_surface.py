"""Publication guard rejects accidental broadening and backend schemas."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from check_public_surface import ROOT, check_surface


class PublicSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name in ['contracts/agent-api.json', 'python/openeight/client.py', 'typescript/src/machine.ts', 'typescript/src/index.ts']:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)

    def test_supported_operations_match(self):
        self.assertEqual(check_surface(self.root), 5)

    def test_backend_schema_is_rejected(self):
        (self.root / 'contract.json').write_text(json.dumps({'openapi': '3.0.3', 'paths': {}}))
        with self.assertRaisesRegex(ValueError, 'schema must stay private'):
            check_surface(self.root)

    def test_yaml_backend_schema_is_rejected(self):
        (self.root / "contract.yaml").write_text("openapi: 3.0.3\npaths: {}\n")
        with self.assertRaisesRegex(ValueError, "schema must stay private"):
            check_surface(self.root)

    def test_extra_rest_surface_is_rejected(self):
        # Construct a synthetic unsupported route rather than listing admin APIs.
        (self.root / 'unrelated.ts').write_text('const path="' + '/api/v1/' + 'unsupported-resource";')
        with self.assertRaisesRegex(ValueError, 'Unsupported API surface'):
            check_surface(self.root)

    def test_missing_operation_is_rejected(self):
        path = self.root / 'contracts/agent-api.json'
        value = json.loads(path.read_text())
        value['operations'].pop()
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'inventory mismatch'):
            check_surface(self.root)


if __name__ == '__main__':
    unittest.main()
