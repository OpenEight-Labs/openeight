"""Offline external control-plane responses; SDK transport and protocols stay real."""

import hashlib
import json
import threading
from uuid import NAMESPACE_URL, uuid5

import httpx


class ExternalControlPlane:
    def __init__(self):
        self.requests = []
        self.tools = {}
        self.lock = threading.RLock()
        self.closed = False
        self.verdict = "allow"
        self.approval_state = "pending"
        self.replay_verdict = None
        self.decisions = {}
        self.field_selectors = []
        self.write_names = set()

    def _readiness(self, tool, *, index=None, contracts=True):
        write = tool["tool"]["capability"] == "write"
        value = {
            "name": tool["tool"]["name"],
            "tool_id": tool["tool"]["id"],
            "tool_version_id": tool["tool_version"]["id"],
            "registration_status": "registered",
            "governed": write,
            "policy_ready": True,
            "eligible": True,
            "reason": "ready" if write else "read_only",
            "governance_revision_id": str(uuid5(NAMESPACE_URL, "governance")),
        }
        if index is not None:
            value["request_index"] = index
        if write and contracts:
            value.update(
                policy_contract_set_digest="a" * 64,
                policy_input_contracts=[
                    {
                        "binding_id": str(uuid5(NAMESPACE_URL, "binding")),
                        "policy_id": str(uuid5(NAMESPACE_URL, "policy")),
                        "policy_version_id": str(
                            uuid5(NAMESPACE_URL, "policy-version")
                        ),
                        "input_digest": "b" * 64,
                        "field_selectors": [
                            {"path": path} for path in self.field_selectors
                        ],
                    }
                ],
            )
        return value

    def send(self, request, deadline):
        with self.lock:
            body = json.loads(request.content)
            operation = request.url.path.rsplit("/", 1)[-1]
            self.requests.append((operation, body))
            if operation == "WriteTelemetry":
                value = {
                    field: [
                        {"id": item["id"], "replayed": False}
                        for item in body.get(field, [])
                    ]
                    for field in ("runs", "events")
                }
            elif operation == "Decide":
                identifier = body["decision_id"]
                replayed = identifier in self.decisions
                if replayed:
                    assert self.decisions[identifier] == body
                else:
                    self.decisions[identifier] = body
                value = {
                    "decision_id": identifier,
                    "run_id": body["run_id"],
                    "verdict": self.replay_verdict
                    if replayed and self.replay_verdict is not None
                    else self.verdict,
                    "reason": "",
                    "replayed": replayed,
                }
            elif operation == "GetApprovalState":
                value = {"state": self.approval_state}
            elif operation == "GetToolReadiness":
                ids = body["tool_version_ids"]
                tools = (
                    list(self.tools.values())
                    if not ids
                    else [self.tools[key] for key in ids]
                )
                value = {
                    "readiness": {
                        "tools": [self._readiness(tool) for tool in tools],
                        "fully_registered": True,
                        "registration_reason": "registered",
                        "fully_policy_ready": True,
                        "policy_reason": "ready",
                    }
                }
            elif operation == "RegisterTools":
                results = []
                for index, fact in enumerate(body["tools"]):
                    identity = str(uuid5(NAMESPACE_URL, fact["name"]))
                    schema = fact["input_schema_json"]
                    version = str(uuid5(NAMESPACE_URL, fact["name"] + schema))
                    write = fact["name"] in self.write_names or fact["name"].rsplit(
                        ".", 1
                    )[-1] in {
                        "task",
                        "write_file",
                        "edit_file",
                        "delete",
                        "write_todos",
                    }
                    tool = {
                        "tool": {
                            "id": identity,
                            "name": fact["name"],
                            "description": fact["description"],
                            "capability": "write" if write else "read",
                            "governed": write,
                            "created_at": "2026-11-08T00:00:00Z",
                        },
                        "tool_version": {
                            "id": version,
                            "tool_id": identity,
                            "version": "1",
                            "content_digest": "sha256:"
                            + hashlib.sha256(schema.encode()).hexdigest(),
                            "input_schema_json": schema,
                            "created_at": "2026-11-08T00:00:00Z",
                        },
                    }
                    if write:
                        tool["tool_version"]["tool_contract_digest"] = (
                            "sha256:" + "1" * 64
                        )
                    self.tools[version] = tool
                    results.append(
                        {
                            "request_index": index,
                            "status": "registered",
                            "reason": "",
                            **tool,
                            "readiness": self._readiness(
                                tool, index=index, contracts=False
                            ),
                        }
                    )
                value = {"results": results}
            else:
                raise AssertionError("Unexpected external operation: " + operation)
            return httpx.Response(200, request=request, json=value)

    def close(self):
        self.closed = True
