"""Custom read-only agent: explicit catalog, readiness and telemetry through the SDK."""

import argparse
from datetime import date, datetime, timezone
import json
import os
from time import perf_counter

from openeight import Client, Event, SDKError, ToolRegistration

from business import lookup_order, recommend_action


def registrations():
    return [
        ToolRegistration(
            name=name,
            description=description,
            input_schema={
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
                "additionalProperties": False,
            },
        )
        for name, description in (
            (
                "lookup_order",
                "Read a sample order's delivery status without changing it.",
            ),
        )
    ]


def publish(client):
    results = client.register_tools(registrations())
    if len(results) != 1 or any(
        item.status not in ("registered", "replayed")
        or item.tool is None
        or item.tool_version is None
        for item in results
    ):
        raise ValueError("catalog_unavailable")
    return {item.tool.name: item for item in results}


def run(client, *, order_id, today, lookup=lookup_order):
    catalog = publish(client)
    if any(
        item.tool.capability != "read" or item.tool.governed
        for item in catalog.values()
    ):
        raise ValueError("classify_lookup_as_read")
    versions = [item.tool_version.id for item in catalog.values()]
    readiness = client.get_tool_readiness(versions)
    if (
        not readiness.fully_registered
        or not readiness.fully_policy_ready
        or len(readiness.tools) != len(versions)
        or {item.tool_version_id for item in readiness.tools} != set(versions)
        or any(
            item.reason != "read_only"
            or item.governed
            or not item.eligible
            or not item.policy_ready
            for item in readiness.tools
        )
    ):
        raise ValueError("classify_lookup_as_read")

    with client.governed_run() as observation:

        def observe(name, operation):
            started_at = datetime.now(timezone.utc)
            started = perf_counter()
            outcome = "success"
            try:
                return operation()
            except Exception:
                outcome = "error"
                raise
            finally:
                registration = catalog.get(name)
                observation.submit_event(
                    Event(
                        name=name,
                        observation_kind="tool_call" if registration else "span",
                        operation_kind="invoke",
                        occurred_at=started_at,
                        outcome=outcome,
                        tool_name=name if registration else None,
                        tool_id=registration.tool.id if registration else None,
                        tool_version_id=registration.tool_version.id
                        if registration
                        else None,
                        span_role=None if registration else "internal",
                        latency_ms=int((perf_counter() - started) * 1000),
                    )
                )

        order = observe("lookup_order", lambda: lookup(order_id))
        reply = observe(
            "recommend_action", lambda: recommend_action(order, today=today)
        )
        run_id = observation.run_id
    # Context exit settles telemetry. No raw customer inputs/outputs are captured.
    return {
        "run_id": run_id,
        "result": reply,
        "delivery": "acknowledged",
        "execution": "read_only",
        "business_effects": [],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("publish", "run"))
    parser.add_argument("order_id", nargs="?", default="ORDER-1002")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    try:
        with Client(
            os.environ["OPENEIGHT_ENDPOINT"],
            api_key=os.environ["OPENEIGHT_API_KEY"],
            agent_name=os.environ["OPENEIGHT_AGENT_NAME"],
            integration="other",
        ) as client:
            result = (
                {
                    "catalog": [
                        {"name": name, "tool_version_id": item.tool_version.id}
                        for name, item in publish(client).items()
                    ]
                }
                if args.command == "publish"
                else run(client, order_id=args.order_id, today=args.as_of)
            )
        print(json.dumps(result, indent=2))
        return 0
    except (SDKError, ValueError, KeyError, OSError):
        print(
            json.dumps(
                {
                    "state": "failed",
                    "explanation": "Check SDK settings, read classification and telemetry delivery.",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
