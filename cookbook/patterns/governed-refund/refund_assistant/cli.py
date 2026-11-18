"""Shared command handling; framework composition stays in the two small runners."""

import argparse
import asyncio
import json

from openeight import ApprovalTimeout, BlockedError, SDKError

from .business import ORDERS, RefundRequest
from .config import configuration, model_client
from .inputs import requested_amount


def blocked(error):
    result = {"state": error.state}
    if error.result is not None:
        result.update(decision_id=error.result.decision_id, run_id=error.result.run_id)
    else:
        result["explanation"] = "Execution blocked without a returned Decision."
    return result


def publication(owner, delivery):
    return {
        "state": "publication",
        "catalog_delivery": delivery.catalog_delivery_state,
        "bindings": [
            {key: row[key] for key in ("child_path", "native_name", "name")}
            for row in owner.catalog_snapshot().bindings
        ],
    }


def delivery_state(delivery):
    if delivery.failed_records or delivery.dropped_records:
        return "failed"
    return "pending" if delivery.pending_records else "acknowledged"


def run_sync(agent, request, command):
    if command == "publish":
        agent.publish()
        return publication(agent, agent.delivery_status())
    try:
        agent.invoke({"messages": [("user", request.prompt)]})
    except ApprovalTimeout as error:
        outcome = blocked(error)
    except BlockedError as error:
        outcome = blocked(error)
    else:
        outcome = {
            "state": "receipt_recorded"
            if request.receipts
            else "missing_refund_attempt"
        }
    return {
        **outcome,
        "receipts": request.receipts,
        "delivery": delivery_state(agent.delivery_status()),
    }


async def run_async(agent, request, command):
    if command == "publish":
        await agent.apublish()
        return publication(agent, agent.delivery_status())
    try:
        await agent.ainvoke({"messages": [("user", request.prompt)]})
    except ApprovalTimeout as error:
        outcome = blocked(error)
    except BlockedError as error:
        outcome = blocked(error)
    else:
        outcome = {
            "state": "receipt_recorded"
            if request.receipts
            else "missing_refund_attempt"
        }
    return {
        **outcome,
        "receipts": request.receipts,
        "delivery": delivery_state(agent.delivery_status()),
    }


def run_cli(build_agent):
    parser = argparse.ArgumentParser(
        description="Simulate one governed partial or full refund request."
    )
    parser.add_argument("command", choices=("publish", "run"))
    parser.add_argument("order", nargs="?", choices=tuple(ORDERS), default="ORD-1041")
    parser.add_argument("--execution", choices=("sync", "async"), default="sync")
    parser.add_argument("--amount", type=requested_amount)
    args = parser.parse_args()
    if args.command == "publish" and args.amount is not None:
        parser.error("--amount is only supported with run")
    request = None
    try:
        request = RefundRequest(args.order, args.amount)
        owner = build_agent(model_client(), request, configuration())
        result = (
            run_sync(owner, request, args.command)
            if args.execution == "sync"
            else asyncio.run(run_async(owner, request, args.command))
        )
    except (SDKError, ValueError, KeyError):
        print(
            json.dumps(
                {
                    "state": "failed",
                    "explanation": "Check configuration, readiness and the selected order.",
                    "receipts": request.receipts if request is not None else [],
                }
            )
        )
        return 1
    except Exception:
        print(
            json.dumps(
                {
                    "state": "failed",
                    "explanation": "Model or execution failed; inspect console telemetry.",
                    "receipts": request.receipts if request is not None else [],
                }
            )
        )
        return 1
    print(json.dumps(result))
    if args.command == "publish":
        return 0 if result["catalog_delivery"] == "acknowledged" else 1
    return (
        0
        if result["state"] == "receipt_recorded"
        and result["delivery"] == "acknowledged"
        else 1
    )
