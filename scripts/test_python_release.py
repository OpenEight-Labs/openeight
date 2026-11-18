"""Regression checks for release authority and version isolation."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]


class PythonReleaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # BaseLoader preserves GitHub's `on` key instead of YAML 1.1 booleans.
        cls.workflow = yaml.load(
            (ROOT / '.github/workflows/python-sdk-release.yml').read_text(),
            Loader=yaml.BaseLoader,
        )

    def test_only_explicit_dispatch_can_publish(self):
        self.assertEqual(set(self.workflow['on']), {'workflow_dispatch'})
        self.assertEqual(self.workflow['on']['workflow_dispatch']['inputs']['publish']['default'], 'false')
        self.assertEqual(self.workflow['permissions'], {'contents': 'read'})
        jobs = self.workflow['jobs']
        self.assertEqual(jobs['publish']['if'], '${{ inputs.publish }}')
        self.assertEqual(jobs['publish']['needs'], ['build', 'qualify'])
        self.assertEqual(jobs['publish']['environment']['name'], 'pypi')
        for name, job in jobs.items():
            if name == 'publish':
                self.assertEqual(job['permissions'], {'id-token': 'write'})
            else:
                self.assertNotIn('id-token', job.get('permissions', {}))

    def test_version_guard(self):
        step = next(step for step in self.workflow['jobs']['build']['steps'] if step.get('id') == 'version')
        code = step['run'].split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        cases = [
            ('refs/tags/python-v1.2.3', '1.2.3', True),
            ('refs/heads/main', '1.2.3', False),
            ('refs/tags/typescript-v1.2.3', '1.2.3', False),
            ('refs/tags/python-v1.2.4', '1.2.3', False),
            ('refs/tags/python-v1.2.3', '1.2.2', False),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / 'sdk/python/openeight'
            package.mkdir(parents=True)
            (package.parent / 'pyproject.toml').write_text('[project]\nname="openeight"\nversion="1.2.3"\n')
            for ref, runtime_version, accepted in cases:
                with self.subTest(ref=ref, runtime_version=runtime_version):
                    (package / '__init__.py').write_text(f'__version__ = "{runtime_version}"\n')
                    result = subprocess.run(
                        [sys.executable, '-c', code], cwd=root,
                        env={**os.environ, 'GITHUB_REF': ref, 'GITHUB_OUTPUT': str(root / 'output')},
                        capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode == 0, accepted, result.stderr)

    def test_published_download_hashes(self):
        steps = self.workflow['jobs']['verify-published']['steps']
        step = next(step for step in steps if step.get('name') == 'Download and verify the production distributions')
        code = step['run'].split("<<'PYCODE'\n", 1)[1].rsplit('\nPYCODE', 1)[0]
        name = 'openeight-1.2.3-py3-none-any.whl'
        original = b'reviewed distribution'
        digest = hashlib.sha256(original).hexdigest()
        metadata = json.dumps({'urls': [{'filename': name, 'url': 'https://files.pythonhosted.org/test.whl', 'digests': {'sha256': digest}}]}).encode()
        for downloaded in (original, b'tampered distribution'):
            with self.subTest(downloaded=downloaded), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'checksums').mkdir()
                (root / 'checksums/SHA256SUMS').write_text(f'{digest}  {name}\n')
                with contextlib.chdir(root), patch.dict(os.environ, SDK_VERSION='1.2.3'), patch(
                    'urllib.request.urlopen', side_effect=[io.BytesIO(metadata), io.BytesIO(downloaded)]
                ):
                    if downloaded == original:
                        exec(compile(code, 'published-download', 'exec'), {})
                        self.assertEqual((root / 'published' / name).read_bytes(), original)
                    else:
                        with self.assertRaises(AssertionError):
                            exec(compile(code, 'published-download', 'exec'), {})


if __name__ == '__main__':
    unittest.main()
