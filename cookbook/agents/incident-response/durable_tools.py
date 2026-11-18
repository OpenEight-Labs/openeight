"""Keyed variants retain public business arguments and reuse destination rules."""

import asyncio
import json
from dataclasses import asdict
from typing import Annotated

from langchain_core.tools import InjectedToolArg, tool
from openeight.deepagents import ReconciliationResult
from pydantic import StrictInt

from contracts import Mutation
from simulator import BusinessError, canonical


def keyed_tools(destination):
    async def apply(
        operation, arguments, service_id, expected_revision, action_id, key
    ):
        receipt = await asyncio.to_thread(
            destination.apply_keyed,
            Mutation(
                action_id,
                service_id,
                operation,
                expected_revision,
                canonical(arguments),
            ),
            idempotency_key=key,
        )
        return canonical(asdict(receipt))

    @tool
    async def restart_replica(
        service_id: str,
        replica_id: str,
        expected_revision: StrictInt,
        action_id: str,
        idempotency_key: Annotated[str, InjectedToolArg],
    ) -> str:
        """Restart one synthetic replica at the expected revision; changes service state."""
        return await apply(
            "restart_replica",
            dict(replica_id=replica_id),
            service_id,
            expected_revision,
            action_id,
            idempotency_key,
        )

    @tool
    async def rollback_release(
        service_id: str,
        target_release: str,
        expected_revision: StrictInt,
        action_id: str,
        idempotency_key: Annotated[str, InjectedToolArg],
    ) -> str:
        """Change the synthetic service to a known release at the expected revision."""
        return await apply(
            "rollback_release",
            dict(target_release=target_release),
            service_id,
            expected_revision,
            action_id,
            idempotency_key,
        )

    @tool
    async def set_replica_count(
        service_id: str,
        replicas: StrictInt,
        expected_revision: StrictInt,
        action_id: str,
        idempotency_key: Annotated[str, InjectedToolArg],
    ) -> str:
        """Change synthetic replica membership at the expected revision."""
        return await apply(
            "set_replica_count",
            dict(replicas=replicas),
            service_id,
            expected_revision,
            action_id,
            idempotency_key,
        )

    return {
        item.name: item
        for item in (restart_replica, rollback_release, set_replica_count)
    }


def receipt_reconciler(destination):
    async def reconcile(attempt):
        try:
            call = json.loads(attempt.native_call_json)
            fields = {
                "restart_replica": "replica_id",
                "rollback_release": "target_release",
                "set_replica_count": "replicas",
            }
            argument = fields[call["name"]]
            args = call["args"]
            if (
                set(args)
                != {
                    "service_id",
                    "expected_revision",
                    "action_id",
                    "idempotency_key",
                    argument,
                }
                or args["idempotency_key"] != attempt.idempotency_key
            ):
                return ReconciliationResult("unknown")
            expected = Mutation(
                args["action_id"],
                args["service_id"],
                call["name"],
                args["expected_revision"],
                canonical({argument: args[argument]}),
            )
            receipt = await asyncio.to_thread(
                destination.lookup, attempt.idempotency_key
            )
            if receipt is None:
                if any(
                    item.mutation.action_id == expected.action_id
                    for item in await asyncio.to_thread(destination.effects)
                ):
                    return ReconciliationResult("unknown")
                return ReconciliationResult("not_executed")
            if receipt.mutation != expected:
                return ReconciliationResult("unknown")
            return ReconciliationResult(
                "confirmed",
                canonical(dict(content=canonical(asdict(receipt)), status="success")),
            )
        except (BusinessError, ValueError, KeyError, TypeError):
            return ReconciliationResult("unknown")

    return reconcile
