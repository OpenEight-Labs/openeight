"""Command-line setup and execution of durable customer support workflows."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from openeight.errors import SDKError

from local import LocalError, Locks, connection
from fixtures import seed_retailer
from money import json_float
from service import DemoService
from configuration import load_binding
from initialization import intended_changes, setup
from storage import SQLiteExecutionStore


def reset(state):
    state = Path(state)
    with Locks(state).hold("lifecycle"):
        deployment = load_binding(state)
        with connection(state / "recovery.sqlite") as db:
            if (
                db.execute("SELECT 1 FROM threads WHERE status!='completed'").fetchone()
                or db.execute(
                    "SELECT 1 FROM outbox WHERE status!='acknowledged'"
                ).fetchone()
                or db.execute(
                    "SELECT 1 FROM ui_commands WHERE status IN ('accepted','running')"
                ).fetchone()
            ):
                raise LocalError("reset_unsettled_work")
        for name in ("retailer.sqlite", "recovery.sqlite"):
            for suffix in ("", "-wal", "-shm"):
                path = state / (name + suffix)
                if path.exists():
                    path.unlink()
        seed_retailer(state, deployment)
        SQLiteExecutionStore(state, deployment).initialize()
    return {"status": "reset", "deployment_id": deployment.id}


def parser():
    p = argparse.ArgumentParser(
        description="Synthetic customer resolution with governed receipts. Requires explicit AGENT_MODEL=<provider>:<model> and an installed provider adapter matching the declared LangChain dependencies for execution."
    )
    p.add_argument(
        "--state",
        type=Path,
        required=True,
        help="Local deployment state directory; SQLite only",
    )
    commands = p.add_subparsers(dest="operation", required=True)
    seed = commands.add_parser("seed")
    seed.set_defaults(operation="seed")
    prepare = commands.add_parser("setup")
    prepare.add_argument("--deployment", required=True)
    prepare.add_argument(
        "--mode", choices=("standalone", "integrated"), default="standalone"
    )
    for name in (
        "endpoint",
        "workspace-id",
        "agent-name",
        "agent-id",
        "api-key-id",
        "api-key-file",
    ):
        prepare.add_argument("--" + name)
    prepare.add_argument("--apply", action="store_true")
    for kind in ("run", "reply", "resume", "continue"):
        command = commands.add_parser(kind)
        command.add_argument("--case" if kind == "run" else "--thread", required=True)
        if kind not in ("resume", "continue"):
            command.add_argument("--message", required=True)
        command.add_argument(
            "--command-id",
            default=None,
            help="Reuse the printed UUID after a lost response",
        )
    control = commands.add_parser("control")
    control.add_argument(
        "--envelope",
        type=Path,
        required=True,
        help="Closed structured synthetic control JSON; retain its UUID for replay",
    )
    for kind in ("status", "receipts"):
        commands.add_parser(kind).add_argument("--thread", required=True)
    commands.add_parser("reset")
    commands.add_parser("setup-status")
    return p


def _print(value):
    print(json.dumps(value, sort_keys=True, indent=2))


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.operation == "setup":
            if not re.fullmatch("[A-Za-z0-9_-]{1,64}", args.deployment):
                raise LocalError("invalid_deployment")
            _print(intended_changes(args.deployment, args.mode))
            if not args.apply:
                return 0
            _print(
                setup(
                    args.state,
                    deployment_id=args.deployment,
                    mode=args.mode,
                    endpoint=(
                        args.endpoint
                        if args.endpoint is not None
                        else os.environ.get("OPENEIGHT_ENDPOINT")
                        if args.mode == "integrated"
                        else None
                    ),
                    workspace_id=args.workspace_id,
                    agent_name=(
                        args.agent_name
                        if args.agent_name is not None
                        else os.environ.get("OPENEIGHT_AGENT_NAME")
                        if args.mode == "integrated"
                        else None
                    ),
                    api_key_file=args.api_key_file,
                    api_key=(
                        os.environ.get("OPENEIGHT_API_KEY")
                        if args.mode == "integrated" and args.api_key_file is None
                        else None
                    ),
                    agent_id=args.agent_id,
                    api_key_id=args.api_key_id,
                    apply=True,
                )
            )
            return 0
        if args.operation == "setup-status":
            deployment = load_binding(args.state)
            _print(
                {
                    "status": "ready",
                    "mode": deployment.mode,
                    "deployment_id": deployment.id,
                }
            )
            return 0
        if args.operation == "seed":
            with Locks(args.state).hold("lifecycle"):
                seed_retailer(args.state, load_binding(args.state))
            _print({"status": "seeded"})
            return 0
        if args.operation == "reset":
            _print(reset(args.state))
            return 0
        service = DemoService(args.state)
        if args.operation in {"status", "receipts"}:
            _print(
                service.snapshot(args.thread)
                if args.operation == "status"
                else service.receipts(args.thread)
            )
            return 0
        if args.operation == "control":
            _print(
                service.control(
                    json.loads(args.envelope.read_text(), parse_float=json_float)
                )
            )
            return 0
        if args.operation == "continue":
            accepted = service.continue_conversation(
                args.thread, args.command_id or str(uuid4())
            )
            _print(accepted)
            _print(asyncio.run(service.execute(accepted["command_id"])))
            return 0
        kind = {"run": "start", "reply": "reply", "resume": "resume"}[args.operation]
        command = {"kind": kind, "command_id": args.command_id or str(uuid4())}
        if kind == "start":
            command.update(case_id=args.case, message=args.message)
        else:
            command["thread_id"] = args.thread
            if kind == "reply":
                command["message"] = args.message
        accepted = service.accept(command)
        _print(accepted)
        snapshot = asyncio.run(service.execute(accepted["command_id"]))
        _print(snapshot)
        return (
            2
            if snapshot["status"] in {"waiting", "waiting_customer"}
            else 3
            if snapshot["status"] == "reconciliation_required"
            else 0
        )
    except LocalError as error:
        _print({"code": error.code, "message": error.code.replace("_", " ")})
        return 3 if "reconciliation" in error.code else 1
    except SDKError:
        _print(
            {
                "code": "execution_unavailable",
                "message": "Inspect persisted state and use explicit recovery. No new credential may resume this deployment.",
            }
        )
        return 3
    except Exception:
        _print(
            {
                "code": "local_failure",
                "message": "Operation unavailable. Inspect persisted state before recovery.",
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
