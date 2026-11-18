"""Explicit local incident commands using public SDK durability APIs only."""

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path

from openeight import Client, ConfigurationError, OpenEightConfig, SDKError
from openeight.deepagents import LocalDurability

from agent import create_incident_agent
from contracts import Settings
from destination import IncidentDestination
from durable_models import durable_models
from durable_tools import receipt_reconciler
from reporting import ReportError, parse_report, validate_report
from simulator import BusinessError, canonical


def _catalog(owner, settings):
    bindings = owner.catalog_snapshot().bindings
    with Client(
        settings.endpoint,
        api_key=settings.credential,
        agent_name=settings.agent_name,
        integration="deepagents",
    ) as client:
        readiness = client.get_tool_readiness([])
    by_name = {item.name: item for item in readiness.tools}
    values = []
    for binding in bindings:
        ready = by_name.get(binding["name"])
        values.append(
            dict(
                child_path=list(binding["child_path"]),
                native_name=binding["native_name"],
                catalog_name=binding["name"],
                tool_id=ready.tool_id if ready else None,
                tool_version_id=ready.tool_version_id if ready else None,
                eligible=ready.eligible if ready else False,
                reason=ready.reason if ready else "registration_required",
            )
        )
    return values


def _delivery(owner):
    status = owner.flush(timeout=30)
    failed = (
        status.failed_records
        or status.dropped_records
        or sum(status.content_capture_failures.values())
    )
    return dict(
        status="failed"
        if failed
        else "incomplete"
        if status.pending_records
        else "drained",
        pending_records=status.pending_records,
        failed_records=status.failed_records,
        dropped_records=status.dropped_records,
    )


def run_durable(
    *,
    action,
    directory,
    revision,
    settings=None,
    scenario=None,
    command_id=None,
    thread_id=None,
):
    root = Path(directory).expanduser()
    destination = IncidentDestination(root / "business")
    if action == "inspect":
        return dict(
            status="local_inspection",
            configuration=destination.configuration(),
            state=asdict(destination.snapshot()),
            effects=[asdict(r) for r in destination.effects()],
        ), 0
    if action != "initialize":
        configuration = destination.configuration()
        if configuration["revision"] != revision:
            raise BusinessError("destination_configuration_mismatch")
        scenario = configuration["scenario"]
    models = durable_models(
        settings=settings, scenario=scenario, destination=destination
    )
    openeight = OpenEightConfig(
        endpoint=settings.endpoint,
        agent_name=settings.agent_name,
        credential=settings.credential,
    )
    durability = LocalDurability(
        directory=root / "execution",
        revision=revision,
        reconcile=receipt_reconciler(destination),
    )
    with create_incident_agent(
        models=models,
        simulator=destination,
        openeight=openeight,
        durability=durability,
    ) as owner:
        if action == "initialize":
            destination.initialize(scenario=scenario, revision=revision)
            try:
                owner.initialize()
            except ConfigurationError as error:
                if error.reason not in {
                    "governance_not_ready",
                    "governance_unconfigured",
                }:
                    raise
                return dict(
                    status="needs_configuration",
                    reason=error.reason,
                    catalog=_catalog(owner, settings),
                ), 3
            return dict(
                status="ready",
                catalog=_catalog(owner, settings),
                delivery=_delivery(owner),
            ), 0
        if action == "start":
            result = owner.start(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": canonical(
                                dict(service_id="checkout", scenario=scenario)
                            ),
                        }
                    ]
                },
                command_id=command_id,
            )
        else:
            result = owner.resume(thread_id=thread_id, command_id=command_id)
        output = dict(
            execution=asdict(result),
            state=asdict(destination.snapshot()),
            effects=[asdict(r) for r in destination.effects()],
            delivery=_delivery(owner),
        )
        if result.status != "completed":
            return output, 3
        if len(result.output) != 1 or not destination.has_report(result.output[0]):
            raise ReportError("report_missing")
        validation = validate_report(
            report=parse_report(result.output[0]),
            before=destination.initial_snapshot(),
            after=destination.snapshot(),
            effects=destination.effects(),
        )
        output["report_validation"] = asdict(validation)
        return output, 0 if validation.status == "passed" and output["delivery"][
            "status"
        ] == "drained" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Persistent scripted incident; explicit initialize/start/resume."
    )
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("initialize", "start", "resume", "inspect"):
        command = commands.add_parser(action)
        command.add_argument("--directory", type=Path, required=True)
        command.add_argument("--revision", required=True)
        if action == "initialize":
            command.add_argument(
                "--scenario",
                choices=("investigate", "restart", "rollback", "scale"),
                required=True,
            )
            command.add_argument("--mode", choices=("scripted",), required=True)
        if action in {"start", "resume"}:
            command.add_argument("--command-id", required=True)
        if action == "resume":
            command.add_argument("--thread-id", required=True)
    args = parser.parse_args(argv)
    try:
        settings = (
            None
            if args.action == "inspect"
            else Settings(
                endpoint=os.environ["OPENEIGHT_ENDPOINT"],
                agent_name=os.environ["OPENEIGHT_AGENT_NAME"],
                credential=os.environ["OPENEIGHT_API_KEY"],
                mode="scripted",
            )
        )
        result, code = run_durable(
            action=args.action,
            directory=args.directory,
            revision=args.revision,
            settings=settings,
            scenario=getattr(args, "scenario", None),
            command_id=getattr(args, "command_id", None),
            thread_id=getattr(args, "thread_id", None),
        )
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return code
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 130
    except ConfigurationError as error:
        print(json.dumps(dict(error_code=error.reason)))
        return (
            3
            if error.reason
            in {"durable_reconciliation_required", "durable_recovery_required"}
            else 2
        )
    except (BusinessError, ReportError) as error:
        print(json.dumps(dict(error_code=error.code)))
        return 1
    except (SDKError, KeyError, ValueError):
        print(json.dumps(dict(error_code="durable_incident_failed")))
        return 1
    except Exception:
        print(json.dumps(dict(error_code="durable_incident_failed")))
        return 1
