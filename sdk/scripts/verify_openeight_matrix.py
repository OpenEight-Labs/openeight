"""Install one built wheel into disposable locked framework environments."""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--environment-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--python", action="append", choices=["3.11", "3.12", "3.13", "3.14"]
    )
    parser.add_argument("--lane", action="append")
    parser.add_argument("--uv", default="uv")
    args = parser.parse_args()
    inventory = json.loads(
        (ROOT / "python/constraints/managed-integrations.json").read_text()
    )
    unknown = set(args.lane or ()) - {lane["name"] for lane in inventory["lanes"]}
    if unknown:
        parser.error("unknown lanes: " + ", ".join(sorted(unknown)))
    evidence = {
        "wheel_sha256": hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
        "lanes": [],
    }
    args.environment_root.mkdir(parents=True, exist_ok=True)
    for python in args.python or inventory["python"]:
        for lane in inventory["lanes"]:
            if args.lane and lane["name"] not in args.lane:
                continue
            environment = args.environment_root / (python + "-" + lane["name"])
            interpreter = environment / "bin/python"
            lock = ROOT / "python/constraints" / lane["lock"]
            steps = []
            commands = [
                [args.uv, "venv", "--clear", "--python", python, str(environment)],
                [
                    str(interpreter),
                    "-c",
                    'import sys, sysconfig; print(sys.version); assert sys.implementation.name == "cpython"; assert sys.version_info.releaselevel == "final"; assert not sysconfig.get_config_var("Py_GIL_DISABLED")',
                ],
                [
                    args.uv,
                    "pip",
                    "install",
                    "--python",
                    str(interpreter),
                    "-r",
                    str(lock),
                    str(args.wheel.resolve()),
                    "mcp==1.30.0",
                    "langgraph-checkpoint-sqlite==3.1.2",
                    "aiosqlite==0.22.1",
                    "langgraph-checkpoint-postgres==3.1.3",
                    "psycopg[binary,pool]==3.3.8",
                    "psycopg-pool==3.3.3",
                ],
                [args.uv, "pip", "check", "--python", str(interpreter)],
                [args.uv, "pip", "freeze", "--python", str(interpreter)],
                [
                    str(interpreter),
                    "-P",
                    "-c",
                    "import pathlib,sys,openeight; origin=pathlib.Path(openeight.__file__).resolve(); print(origin); assert origin.is_relative_to(pathlib.Path(sys.prefix).resolve())",
                ],
                [
                    str(interpreter),
                    "-m",
                    "pytest",
                    str(ROOT / "python/tests"),
                    "-q",
                ],
            ]
            for command in commands:
                result = subprocess.run(
                    command,
                    check=False,
                    cwd=ROOT,
                    env={
                        **{
                            key: value
                            for key, value in os.environ.items()
                            if key != "PYTHONPATH"
                        },
                        "LANGGRAPH_STRICT_MSGPACK": "true",
                    },
                    capture_output=True,
                    text=True,
                )
                steps.append(
                    {
                        "command": command,
                        "exit": result.returncode,
                        "output": result.stdout + result.stderr,
                    }
                )
                if result.returncode:
                    break
            evidence["lanes"].append(
                {
                    "python": python,
                    "name": lane["name"],
                    "steps": steps,
                    "passed": all(step["exit"] == 0 for step in steps)
                    and len(steps) == len(commands),
                }
            )
            args.output.write_text(json.dumps(evidence, indent=2) + "\n")
            print(python, lane["name"], evidence["lanes"][-1]["passed"], flush=True)
    return 0 if all(lane["passed"] for lane in evidence["lanes"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
