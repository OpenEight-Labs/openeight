"""Exercise public SDK clients by replacing HTTPX's network sends only."""

import base64
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openeight import APIError, TransportError


def wire(value):
    """Encode public catalog/result records as their documented HTTP shapes."""
    if is_dataclass(value):
        value = {
            field.name: getattr(value, field.name)
            for field in fields(value)
            if getattr(value, field.name) is not None
        }
    elif isinstance(value, SimpleNamespace):
        value = vars(value)
    if isinstance(value, Mapping):
        result = {key: wire(item) for key, item in value.items()}
        if "input_schema" in result:
            result["input_schema_json"] = json.dumps(result.pop("input_schema"))
        return result
    if isinstance(value, (list, tuple)):
        return [wire(item) for item in value]
    return value


class ControlHTTP:
    """One stateful service per SDK client; reject every unexpected network call.

    Models are scripted by the tests. Both HTTPX send APIs are intercepted so the
    actual SDK still serializes, validates, polls and settles its HTTP requests.
    No OpenEight implementation modules or client methods are replaced.
    """

    def __init__(self, service_factory):
        self.factory = service_factory
        self.clients = {}
        self.calls = []
        self.catalog_versions = {}

    def respond(self, client, request):
        if request.url.host not in {"localhost", "127.0.0.1", "control.example"}:
            raise AssertionError("Unexpected external network request")
        service = self.clients.setdefault(client, None)
        if service is None:
            service = self.factory()
            service.catalog_versions = self.catalog_versions
            self.clients[client] = service
        body = json.loads(request.content)
        operation = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((operation, body))
        try:
            if operation == "RegisterTools":
                facts = tuple(
                    SimpleNamespace(
                        name=item["name"],
                        description=item["description"],
                        input_schema=json.loads(item["input_schema_json"]),
                    )
                    for item in body["tools"]
                )
                registrations = service.register_tools(facts)
                for item in registrations:
                    if item.tool_version is not None:
                        self.catalog_versions[item.tool_version.id] = item
                result = {"results": wire(registrations)}
                for row in result["results"]:
                    if "readiness" in row:
                        row["readiness"].pop("policy_input_contracts", None)
                        row["readiness"].pop("policy_contract_set_digest", None)
            elif operation == "GetToolReadiness":
                readiness = wire(service.get_tool_readiness(body["tool_version_ids"]))
                readiness = {
                    "fully_registered": True,
                    "registration_reason": "registered",
                    "fully_policy_ready": True,
                    "policy_reason": "ready",
                    **readiness,
                }
                result = {"readiness": readiness}
            elif operation == "Decide":
                request_body = dict(body)
                request_body["input"] = json.loads(request_body.pop("input_json"))
                request_body["selected_values"] = json.loads(
                    request_body.pop("selected_values_json")
                )
                result = wire(service.decide(**request_body))
            elif operation == "GetApprovalState":
                result = {"state": service.approval_state(body["decision_id"])}
            elif operation == "WriteTelemetry":
                service.write_telemetry(**body)
                result = {
                    key: [{"id": item["id"], "replayed": False} for item in body[key]]
                    for key in ("runs", "events")
                }
            else:
                raise AssertionError(f"Unexpected control operation: {operation}")
        except APIError as error:
            details = []
            if error.code == "AUTHORIZATION_STALE":
                reason, domain = error.code.encode(), b"openeight.govern"
                payload = (
                    bytes([10, len(reason)])
                    + reason
                    + bytes([18, len(domain)])
                    + domain
                )
                details = [
                    {
                        "type": "google.rpc.ErrorInfo",
                        "value": base64.b64encode(payload).decode(),
                    }
                ]
            return httpx.Response(
                error.status_code,
                json={
                    "code": error.category,
                    "message": "Synthetic service error",
                    "details": details,
                },
                request=request,
            )
        except TransportError:
            raise httpx.ConnectError(
                "Synthetic transport failure", request=request
            ) from None
        return httpx.Response(200, json=result, request=request)

    def __enter__(self):
        control = self

        def send(client, request, **kwargs):
            return control.respond(client, request)

        async def async_send(client, request, **kwargs):
            return control.respond(client, request)

        self.sync = patch.object(httpx.Client, "send", send)
        self.asynchronous = patch.object(httpx.AsyncClient, "send", async_send)
        self.sync.start()
        self.asynchronous.start()
        return self

    def __exit__(self, *exc):
        self.asynchronous.stop()
        self.sync.stop()
