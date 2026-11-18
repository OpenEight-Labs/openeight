#!/usr/bin/env python3
"""Set up and run cookbook agents, workflows and tool patterns."""

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from urllib.parse import urlsplit

if sys.version_info < (3, 11):
    raise SystemExit("Use Python 3.11–3.14 to run the cookbook.")
import tomllib

ROOT = Path(__file__).resolve().parent


def recipes():
    with (ROOT / "recipes.toml").open("rb") as source:
        sections = tomllib.load(source)
    catalog = {}
    for section, kind in (
        ("agents", "agent"),
        ("patterns", "pattern"),
    ):
        for name, recipe in sections.get(section, {}).items():
            if name in catalog:
                raise ValueError("Recipe names must be unique across categories.")
            catalog[name] = {**recipe, "kind": kind}
    return catalog


def environment(directory, *, env_file=None):
    """Shell values win over literal file assignments; files are never executed."""
    values = dict(os.environ)
    source = env_file if env_file is not None else directory / ".env"
    if env_file is not None and not source.is_file():
        raise ValueError("Environment file does not exist.")
    if source.is_file():
        for number, line in enumerate(source.read_text().splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, raw = line.removeprefix("export ").partition("=")
            key = key.strip()
            if not separator or not key.isidentifier() or not key.isascii():
                raise ValueError(f"Invalid environment assignment on line {number}.")
            parts = shlex.split(raw, comments=True)
            if len(parts) > 1:
                raise ValueError(f"Quote values containing spaces on line {number}.")
            values.setdefault(key, parts[0] if parts else "")
    # Incident Response exposes explicit provider settings for direct CLI use.
    for target, source in {
        "INCIDENT_PROVIDER_MODEL": "AGENT_MODEL",
        "INCIDENT_PROVIDER_API_KEY": "AGENT_MODEL_API_KEY",
    }.items():
        if values.get(source) and not values.get(target):
            values[target] = values[source]
    return values


def configuration_issues(recipe, values, *, provider=False):
    required = ["OPENEIGHT_ENDPOINT", "OPENEIGHT_AGENT_NAME", "OPENEIGHT_API_KEY"]
    model_required = recipe["requires_model"] or provider
    if model_required:
        required += ["AGENT_MODEL", "AGENT_MODEL_API_KEY"]
    missing = [
        key
        for key in required
        if not values.get(key, "").strip() or "<" in values[key] or ">" in values[key]
    ]
    issues = [f"Set {key}." for key in missing]
    try:
        origin = urlsplit(values.get("OPENEIGHT_ENDPOINT", ""))
        secure = origin.scheme == "https" or (
            recipe["integration"] in ("managed", "sdk")
            and origin.scheme == "http"
            and origin.hostname in ("localhost", "127.0.0.1", "::1")
        )
        if (
            not secure
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.path not in ("", "/")
            or origin.query
            or origin.fragment
        ):
            issues.append(
                "Use a valid OpenEight API origin with a supported transport."
            )
    except ValueError:
        issues.append("Use a valid OpenEight API origin.")
    if model_required and "AGENT_MODEL" not in missing:
        provider_name, separator, model = values["AGENT_MODEL"].partition(":")
        if not separator or not provider_name.strip() or not model.strip():
            issues.append("Set AGENT_MODEL to an explicit provider:model.")
    if recipe["integration"] == "otel" and not values.get(
        "OPENEIGHT_ENDPOINT", ""
    ).startswith("https://"):
        issues.append("OTEL export requires an HTTPS endpoint.")
    return issues


def execute(command, *, directory, values=None):
    return subprocess.run(command, cwd=directory, env=values, check=False).returncode


def main(argv=None):
    catalog = recipes()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="Choose a business agent")
    listing.add_argument(
        "--all", action="store_true", help="Include focused integration patterns"
    )
    for command in ("setup", "check", "sample", "run", "test"):
        sub = commands.add_parser(command)
        sub.add_argument("agent", choices=catalog, metavar="recipe")
        if command == "setup":
            sub.add_argument(
                "--python",
                default=sys.executable,
                help="Python 3.11–3.14 interpreter for the recipe's .venv",
            )
        elif command in ("check", "run"):
            sub.add_argument(
                "--env-file",
                type=Path,
                help="Literal env file (default: the selected agent's .env)",
            )
            if command == "check":
                sub.add_argument(
                    "--provider",
                    action="store_true",
                    help="Also check incident provider configuration",
                )
    # Everything after -- belongs to the agent, not the cookbook launcher.
    arguments = list(sys.argv[1:] if argv is None else argv)
    boundary = arguments.index("--") if "--" in arguments else len(arguments)
    args = parser.parse_args(arguments[:boundary])
    extra = arguments[boundary + 1 :]
    if extra and args.command not in ("run", "test", "sample"):
        parser.error("Only run, test and sample accept arguments after --.")
    if args.command == "list":
        for name, recipe in catalog.items():
            if not args.all and recipe["kind"] != "agent":
                continue
            print(
                f"{name:20} {recipe['kind']:8} {recipe['domain']:18} {recipe['framework']:12} {recipe['summary']}"
            )
        return 0
    recipe = catalog[args.agent]
    directory = ROOT / recipe["path"]
    interpreter = (
        directory
        / ".venv"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    if args.command == "setup":
        if not interpreter.exists():
            status = execute(
                [args.python, "-m", "venv", str(directory / ".venv")],
                directory=directory,
            )
            if status:
                return status
        command = [
            str(interpreter),
            "-m",
            "pip",
            "install",
            "-r",
            recipe["requirements"],
        ]
        if recipe.get("install"):
            command += ["-e", recipe["install"]]
        return execute(command, directory=directory)
    if args.command == "sample":
        # Samples use only Python's standard library and local business data.
        return execute([sys.executable, *recipe["sample"], *extra], directory=directory)
    values = (
        dict(os.environ)
        if args.command == "test"
        else environment(directory, env_file=getattr(args, "env_file", None))
    )
    if args.command == "check":
        issues = configuration_issues(recipe, values, provider=args.provider)
        if not interpreter.is_file():
            issues.append(f"Run python cookbook.py setup {args.agent}.")
        print(
            json.dumps(
                {
                    "recipe": args.agent,
                    "configuration": "incomplete" if issues else "present",
                    "issues": issues,
                    "live_connectivity": "not_checked",
                    "governance_readiness": "not_checked",
                },
                indent=2,
            )
        )
        return 1 if issues else 0
    if not interpreter.is_file():
        print(f"Run python cookbook.py setup {args.agent} first.", file=sys.stderr)
        return 1
    if args.command == "test":
        entry = recipe.get("test_entry", ["-m", "pytest", "tests"])
        return execute(
            [str(interpreter), *entry, *extra], directory=directory, values=values
        )
    return execute(
        [str(interpreter), *recipe["entry"], *extra], directory=directory, values=values
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError):
        raise SystemExit(
            "Cookbook command failed. Check the interpreter, file paths and literal env assignments."
        ) from None
