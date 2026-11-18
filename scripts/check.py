"""Run structural SDK checks and dependency-free cookbook checks."""
from pathlib import Path
import subprocess
import sys
ROOT = Path(__file__).resolve().parents[1]
commands = [
    ("sdk", [sys.executable, "scripts/generate_error_catalog.py", "--check"]),
    ("sdk", [sys.executable, "scripts/check_framework_registry.py"]),
    ("sdk", [sys.executable, "scripts/check_public_surface.py"]),
    ("sdk", [sys.executable, "-m", "unittest", "discover", "-s", "scripts", "-p", "test_*.py"]),
    ("cookbook", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]),
    ("cookbook", [sys.executable, "-m", "unittest", "discover", "-s", "patterns/order-lookup/tests", "-v"]),
]
failed = []
for directory, command in commands:
    result = subprocess.run(command, cwd=ROOT / directory, check=False)
    if result.returncode:
        failed.append((directory, command, result.returncode))
if failed:
    print(failed)
    sys.exit(1)
