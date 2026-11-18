"""Credential-free arguments and JSON output with stable exit semantics."""

import argparse
import asyncio
import json
import math
import os
import sys
from dataclasses import asdict
from collections.abc import Sequence
from contracts import Settings
from models import SCENARIOS
from runner import run_catalog, run_scenario


def exit_code(result):
    status = result.invocation.status
    if status in ("blocked", "approval_timeout"):
        return 3
    if status == "business_rejected":
        return 4
    if (
        status != "completed"
        or result.delivery.status != "drained"
        or any(c.status == "failed" for c in result.checks)
    ):
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if arguments and arguments[0] == "durable":
        from durable_cli import main as durable_main

        return durable_main(arguments[1:])
    parser = argparse.ArgumentParser(
        description="Governed synthetic incident; ordinary runs start fresh, durable commands retain state."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("durable", help="Persistent scripted incident commands")
    for command in ("catalog", "run"):
        sub = subparsers.add_parser(command)
        if command == "run":
            sub.add_argument("scenario", choices=SCENARIOS)
        sub.add_argument("--mode", choices=("scripted", "provider"), required=True)
        sub.add_argument(
            "--capture-content",
            choices=("none", "inputs", "outputs", "all"),
            default="none",
        )
        sub.add_argument(
            "--redaction", choices=("none", "synthetic-sentinels"), default="none"
        )
        sub.add_argument("--approval-timeout", type=float, default=300.0)
    args = parser.parse_args(arguments)
    try:
        settings = Settings(
            endpoint=os.environ["OPENEIGHT_ENDPOINT"],
            agent_name=os.environ["OPENEIGHT_AGENT_NAME"],
            credential=os.environ["OPENEIGHT_API_KEY"],
            mode=args.mode,
            provider_model=os.getenv("INCIDENT_PROVIDER_MODEL"),
            provider_credential=os.getenv("INCIDENT_PROVIDER_API_KEY"),
            approval_timeout_seconds=args.approval_timeout,
            capture_content=args.capture_content,
            redaction=args.redaction,
        )
        if not math.isfinite(args.approval_timeout) or args.approval_timeout <= 0:
            raise ValueError()
        # Validate credentials and explicit provider settings before entering the event loop.
        from content import build_configuration
        from models import build_models

        build_configuration(settings=settings)
        build_models(
            settings=settings, scenario=getattr(args, "scenario", "investigate")
        )
    except Exception:
        print(json.dumps({"error_code": "invalid_configuration"}))
        return 2
    try:
        result = asyncio.run(
            run_catalog(settings=settings)
            if args.command == "catalog"
            else run_scenario(name=args.scenario, settings=settings)
        )
        print(json.dumps(asdict(result), sort_keys=True, allow_nan=False))
        if args.command == "catalog":
            return (
                0
                if result.publication_complete
                and result.entries
                and not result.error_code
                else 1
            )
        return exit_code(result)
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 130
    except Exception:
        print(json.dumps({"error_code": "execution_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
