"""Exercise the actual public SDK against an HTTP control-service double."""

from datetime import date
import json
from pathlib import Path
import os
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    import httpx
    from openeight import Client, SDKError
    import agent
except ModuleNotFoundError:
    if os.environ.get("COOKBOOK_REQUIRE_SDK") == "1":
        raise
    Client = None

TOOL = "00000000-0000-4000-8000-000000000001"
VERSION = "00000000-0000-4000-8000-000000000002"


class Control:
    ready = True
    fail_delivery = False

    def __init__(self):
        self.requests = []

    def __call__(self, request):
        body = json.loads(request.content)
        path = request.url.path.rsplit("/", 1)[-1]
        self.requests.append((path, body))
        row = {
            "request_index": 0,
            "name": "lookup_order",
            "tool_id": TOOL,
            "tool_version_id": VERSION,
            "registration_status": "registered",
            "governed": False,
            "policy_ready": self.ready,
            "eligible": self.ready,
            "reason": "read_only" if self.ready else "classification_required",
        }
        if path == "RegisterTools":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "request_index": 0,
                            "status": "registered",
                            "reason": "",
                            "tool": {
                                "id": TOOL,
                                "name": "lookup_order",
                                "description": body["tools"][0]["description"],
                                "capability": "read",
                                "governed": False,
                                "created_at": "2026-11-15T00:00:00Z",
                            },
                            "tool_version": {
                                "id": VERSION,
                                "tool_id": TOOL,
                                "version": "1",
                                "content_digest": "sha256:" + "0" * 64,
                                "input_schema_json": body["tools"][0][
                                    "input_schema_json"
                                ],
                                "created_at": "2026-11-15T00:00:00Z",
                            },
                            "readiness": {
                                **row,
                                "policy_ready": True,
                                "eligible": True,
                                "reason": "read_only",
                            },
                        }
                    ]
                },
            )
        if path == "GetToolReadiness":
            return httpx.Response(
                200,
                json={
                    "readiness": {
                        "tools": [row],
                        "fully_registered": True,
                        "registration_reason": "registered",
                        "fully_policy_ready": self.ready,
                        "policy_reason": "ready"
                        if self.ready
                        else "classification_required",
                    }
                },
            )
        if path == "WriteTelemetry":
            if self.fail_delivery:
                return httpx.Response(
                    503, json={"code": "unavailable", "message": "Unavailable"}
                )
            return httpx.Response(
                200,
                json={
                    key: [{"id": item["id"], "replayed": False} for item in body[key]]
                    for key in ("runs", "events")
                },
            )
        raise AssertionError(path)


@unittest.skipIf(Client is None, "Install openeight for SDK integration tests")
class SDKTests(unittest.TestCase):
    def client(self, control):
        return Client(
            "https://control.example",
            api_key="ctk_test",
            agent_name="orders",
            integration="other",
            transport=httpx.MockTransport(control),
        )

    def test_read_only_run_has_real_sdk_events_without_a_decision_or_raw_content(self):
        control = Control()
        with self.client(control) as client:
            result = agent.run(client, order_id="ORDER-1002", today=date(2026, 1, 15))
        self.assertEqual(result["result"]["action"], "investigate_delayed_delivery")
        self.assertEqual(result["delivery"], "acknowledged")
        paths = [path for path, _ in control.requests]
        self.assertNotIn("Decide", paths)
        events = [
            event
            for path, body in control.requests
            if path == "WriteTelemetry"
            for event in body["events"]
        ]
        self.assertEqual(
            {event["name"] for event in events}, {"lookup_order", "recommend_action"}
        )
        self.assertTrue(
            all(
                json.loads(event.get("input_json") or "null") in (None, {})
                and json.loads(event.get("output_json") or "null") in (None, {})
                for event in events
            )
        )
        runs = [
            run
            for path, body in control.requests
            if path == "WriteTelemetry"
            for run in body["runs"]
        ]
        self.assertEqual(runs[-1]["status"], "completed")

    def test_unready_classification_stops_before_lookup_and_run(self):
        control = Control()
        control.ready = False
        lookup = Mock()
        with self.client(control) as client, self.assertRaises(ValueError):
            agent.run(
                client, order_id="ORDER-1002", today=date(2026, 1, 15), lookup=lookup
            )
        lookup.assert_not_called()
        self.assertNotIn("WriteTelemetry", [path for path, _ in control.requests])

    def test_delivery_failure_is_not_acknowledged_success(self):
        control = Control()
        control.fail_delivery = True
        with self.client(control) as client, self.assertRaises(SDKError):
            agent.run(client, order_id="ORDER-1002", today=date(2026, 1, 15))
