"""Public recipes must not depend on OpenEight implementation modules."""

import ast
from pathlib import Path
import unittest


class PublicSDKBoundaryTests(unittest.TestCase):
    def test_agents_patterns_and_test_helpers_use_public_sdk_interfaces(self):
        root = Path(__file__).resolve().parents[1]
        violations = []
        for directory in ("agents", "workflows", "patterns", "tests"):
            for path in (root / directory).rglob("*.py"):
                if any(
                    part.startswith(".") or part == "__pycache__"
                    for part in path.relative_to(root).parts
                ):
                    continue
                for node in ast.walk(ast.parse(path.read_text())):
                    modules = []
                    if isinstance(node, ast.Import):
                        modules = [item.name for item in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        modules = [node.module]
                        if node.module.split(".")[0] == "openeight":
                            modules += [
                                node.module + "." + item.name for item in node.names
                            ]
                    for module in modules:
                        if module.split(".")[0] == "openeight" and any(
                            part.startswith("_") for part in module.split(".")[1:]
                        ):
                            violations.append(
                                f"{path.relative_to(root)}:{node.lineno}: {module}"
                            )
                    if isinstance(node, ast.Attribute) and node.attr == "_owner":
                        violations.append(
                            f"{path.relative_to(root)}:{node.lineno}: private SDK owner"
                        )
        self.assertEqual(violations, [], "\n".join(violations))
