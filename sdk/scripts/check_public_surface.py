"""Check the agent-only publication inventory; not a secret or security scan."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIRECTORIES = {'.git', 'node_modules', '__pycache__', '.pytest_cache', '.ruff_cache', '.venv', '.local', 'output', 'build', 'dist', 'test-results', 'playwright-report'}
PATH_PATTERN = re.compile(r'/api/v1/[A-Za-z0-9_./{}-]+')


def check_surface(root):
    inventory = json.loads((root / 'contracts/agent-api.json').read_text())
    operations = inventory['operations']
    expected = {operation['path'] for operation in operations}
    if inventory['format_version'] != 1 or len(expected) != len(operations):
        raise ValueError('Invalid or duplicate operation inventory')
    if any(operation['method'] != 'POST' for operation in operations):
        raise ValueError('Agent operations must use POST')
    for source in ['python/openeight/client.py', 'typescript/src/machine.ts']:
        actual = set(PATH_PATTERN.findall((root / source).read_text()))
        if actual != expected:
            raise ValueError(f'Operation inventory mismatch: {source}')
    for path in root.rglob('*'):
        relative = path.relative_to(root)
        if any(part in EXCLUDED_DIRECTORIES or part.endswith('.egg-info') for part in relative.parts):
            continue
        if not path.is_file():
            continue
        if path.suffix == '.json':
            value = json.loads(path.read_text())
            if isinstance(value, dict) and ('openapi' in value or 'swagger' in value):
                raise ValueError(f'Backend API schema must stay private: {relative}')
        if path.suffix in {'.yaml', '.yml'} and re.search(r'(?m)^\s*(?:openapi|swagger)\s*:', path.read_text()):
            raise ValueError(f'Backend API schema must stay private: {relative}')
        if path.suffix in {'.py', '.ts', '.md', '.json', '.yaml', '.yml'}:
            # Full concrete REST paths are excluded even from docs and fixtures.
            if set(PATH_PATTERN.findall(path.read_text())) - expected:
                raise ValueError(f'Unsupported API surface: {relative}')
    index = (root / 'typescript/src/index.ts').read_text()
    if './human.ts' in index or './generated/' in index:
        raise ValueError('Public TypeScript exports include an excluded client or schema')
    for name in ['typescript/src/human.ts', 'typescript/src/generated/types.ts', 'typescript/src/generated/human-routes.ts']:
        if (root / name).exists():
            raise ValueError(f'Excluded contract artifact: {name}')
    return len(expected)


if __name__ == '__main__':
    print(f'Public surface verified: {check_surface(ROOT)} agent operations; no backend OpenAPI or REST surface')
