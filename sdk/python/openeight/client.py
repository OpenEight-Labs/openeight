"""Synchronous machine client for the OpenEight Connect APIs."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from types import TracebackType
from typing import TYPE_CHECKING, Any, Mapping, Sequence
from urllib.parse import urlsplit

import httpx

from openeight._connect_errors import governance_reason
from openeight._direct import safe_direct_errors
from openeight._forbidden_fields import FORBIDDEN_TELEMETRY_SELECTORS
from openeight._json import thaw_json
from openeight._protocol_constants import (
    AGENT_NAME_ENV,
    AGENT_NAME_HEADER,
    API_KEY_ENV,
    API_KEY_PREFIX,
    BASE_URL_ENV,
)
from openeight._sdk_version import resolve_sdk_version
from openeight._time import utc_timestamp
from openeight._validation import validate_timeout
from openeight.errors import (
    _CONNECT_CODES,
    _REST_CATEGORIES,
    _REST_ERROR_DEFINITIONS,
    APIError,
    ConfigurationError,
    ProtocolError,
    SDKError,
    TransportError,
    _fresh_safe_error,
    _safe_error_descriptor,
)
from openeight.integration import AgentIntegration
from openeight.telemetry import BufferedObserveWriter, _terminal_replay_operation
from openeight.tools import (
    AgentReadiness,
    RegistrationResult,
    ToolRegistration,
    parse_readiness,
    parse_registration,
)
from openeight.types import DecisionResult, Event, validate_decision_result

if TYPE_CHECKING:
    from openeight.decisions import PreparedDecision
    from openeight.run import GovernedRun, RunCheckpoint, RunIdentity


_AGENT_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,127}$")
_REST_REASON_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_CLOSE_TIMEOUT_SECONDS = 10.0


def _send_sync_request_with_timeout(
    http: httpx.Client,
    request: httpx.Request,
    timeout: float,
    settled: threading.Event | None = None,
) -> tuple[httpx.Response | None, bool]:
    """Wait at most timeout seconds for a synchronous transport to return.

    The daemon call cannot be cancelled. Facade requests instead use the owned
    DeadlineHTTPTransport, which cancels and settles native I/O.
    """
    done = settled if settled is not None else threading.Event()
    responses: list[httpx.Response] = []
    failed: list[bool] = []

    def invoke() -> None:
        try:
            responses.append(http.send(request))
        except BaseException:
            failed.append(True)
        finally:
            done.set()

    threading.Thread(target=invoke, daemon=True).start()
    if not done.wait(timeout):
        return None, True
    return (responses[0] if responses else None), bool(failed)


def _configuration_error(reason: str) -> ConfigurationError:
    return ConfigurationError(reason)


def _validate_base_url(value: Any) -> str:
    if value is None:
        raise _configuration_error("missing_base_url")
    if not isinstance(value, str):
        raise _configuration_error("invalid_base_url")

    missing = False
    valid = False
    normalized_url: str | None = None
    try:
        text = str(value)
        missing = not text.strip()
        if missing or any(character.isspace() for character in text):
            raise ValueError
        parsed = urlsplit(text)
        port = parsed.port
        valid = not (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or port is not None
            and not 0 < port <= 65535
        )
        if valid:
            url = httpx.URL(text.rstrip("/"))
            if url.scheme == "http" and url.host not in {"localhost", "::1"}:
                if not ipaddress.IPv4Address(url.host).is_loopback:
                    raise ValueError
            normalized_url = str(url)
    except Exception:
        pass
    if missing:
        raise _configuration_error("missing_base_url") from None
    if not valid or normalized_url is None:
        raise _configuration_error("invalid_base_url") from None
    return normalized_url


def _validate_api_key(value: Any) -> str:
    if value is None:
        raise _configuration_error("missing_api_key")
    if not isinstance(value, str):
        raise _configuration_error("invalid_api_key")
    missing = False
    valid = False
    normalized: str | None = None
    try:
        normalized = str(value)
        missing = not normalized.strip()
        valid = normalized.startswith(API_KEY_PREFIX)
    except Exception:
        pass
    if missing:
        raise _configuration_error("missing_api_key") from None
    if not valid or normalized is None:
        raise _configuration_error("invalid_api_key") from None
    return normalized


def _validate_agent_name(value: Any) -> str:
    if value is None:
        raise _configuration_error("missing_agent_name")
    if not isinstance(value, str):
        raise _configuration_error("invalid_agent_name")
    missing = False
    valid = False
    normalized: str | None = None
    try:
        normalized = str(value)
        missing = not normalized.strip()
        valid = _AGENT_NAME_PATTERN.fullmatch(normalized) is not None
    except Exception:
        pass
    if missing:
        raise _configuration_error("missing_agent_name") from None
    if not valid or normalized is None:
        raise _configuration_error("invalid_agent_name") from None
    return normalized


def _string_value(value: Any, reason: str) -> str:
    normalized: str | None = None
    try:
        if isinstance(value, str):
            normalized = str(value)
    except Exception:
        pass
    if normalized is None:
        raise _configuration_error(reason) from None
    return normalized


def _nonblank_string(value: Any, reason: str) -> str:
    normalized = _string_value(value, reason)
    if not normalized.strip():
        raise _configuration_error(reason)
    return normalized


def _uuid7_or_new(value: str, reason: str) -> str:
    normalized = _string_value(value, reason)
    parsed = None
    try:
        if normalized:
            parsed = uuid.UUID(normalized)
    except Exception:
        pass
    if not normalized:
        from openeight.telemetry import uuid7

        return uuid7()
    if parsed is None:
        raise _configuration_error(reason) from None
    if parsed.version != 7 or parsed.variant != uuid.RFC_4122:
        raise _configuration_error(reason)
    return str(parsed)


def _uuid7_required(value: Any, reason: str) -> str:
    normalized = _string_value(value, reason)
    if not normalized:
        raise _configuration_error(reason) from None
    return _uuid7_or_new(normalized, reason)


def _validate_uuid(value: Any, reason: str) -> str:
    valid = False
    try:
        parsed = uuid.UUID(value)
        valid = (
            parsed.variant == uuid.RFC_4122 and parsed.int != 0 and str(parsed) == value
        )
    except Exception:
        valid = False
    if not valid:
        raise _configuration_error(reason)
    return value


def _serialize_tool_facts(registration: Any) -> dict[str, Any]:
    schema = thaw_json(registration.input_schema)
    body = {
        "name": registration.name,
        "description": registration.description,
        "input_schema_json": json.dumps(
            schema,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    }
    if registration.expected_tool_id is not None:
        body["expected_tool_id"] = registration.expected_tool_id
        body["expected_current_version_id"] = registration.expected_current_version_id
    return body


def _normalize_source_timestamp(value: datetime | str) -> str:
    normalized: str | None = None
    try:
        parsed: datetime | None = None
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            text = str(value)
            if _RFC3339_PATTERN.fullmatch(text) is not None:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if (
            parsed is not None
            and parsed.tzinfo is not None
            and parsed.utcoffset() is not None
        ):
            normalized = (
                parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            )
    except Exception:
        pass
    if normalized is None:
        raise _configuration_error("invalid_source_time") from None
    return normalized


def _validate_json_payload(value: Any, reason: str) -> None:
    invalid = False
    try:
        json.dumps(value, allow_nan=False)
    except Exception:
        invalid = True
    if invalid:
        raise _configuration_error(reason) from None


def _is_valid_json_value(value: Any, active_containers: set[int]) -> bool:
    value_type = type(value)
    if value is None or value_type in {bool, str}:
        return True
    if value_type is int:
        return -(1 << 63) <= value <= (1 << 63) - 1
    if value_type is float:
        return math.isfinite(value)
    if value_type not in {dict, list}:
        return False

    identity = id(value)
    if identity in active_containers:
        return False
    active_containers.add(identity)
    try:
        if value_type is list:
            return all(_is_valid_json_value(item, active_containers) for item in value)
        return all(
            type(key) is str and _is_valid_json_value(item, active_containers)
            for key, item in value.items()
        )
    finally:
        active_containers.remove(identity)


def _validate_telemetry_items(value: Any, reason: str) -> list[dict[str, Any]]:
    if value is None:
        return []

    valid = False
    forbidden_scope = False
    try:
        valid = type(value) is list and all(
            type(item) is dict and _is_valid_json_value(item, set()) for item in value
        )
        if valid:
            value = json.loads(
                json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8")
            )
            forbidden_scope = any(
                not FORBIDDEN_TELEMETRY_SELECTORS.isdisjoint(item) for item in value
            )
    except Exception:
        valid = False
    if not valid:
        raise _configuration_error(reason) from None
    if forbidden_scope:
        raise _configuration_error("invalid_telemetry_scope") from None
    from openeight._telemetry_validation import validate_event, validate_run

    for item in value:
        (validate_run if reason == "invalid_runs" else validate_event)(item)
    return value


def _normalize_connect_response(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            _camel_to_snake_case(key): _normalize_connect_response(value)
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [_normalize_connect_response(value) for value in obj]
    return obj


def _camel_to_snake_case(name: str) -> str:
    first = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    second = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", first)
    return second.lower()


def _validate_telemetry_acknowledgement(
    data: Any,
    request_body: dict[str, list[dict[str, Any]]],
    status_code: int,
) -> None:
    valid = type(data) is dict and set(data) == {"runs", "events"}
    if valid:
        for field in ("runs", "events"):
            requested = request_body[field]
            results = data[field]
            requested_ids = [write.get("id") for write in requested]
            result_ids: list[Any] = []
            valid = (
                type(results) is list
                and all(type(item) is str and bool(item) for item in requested_ids)
                and len(set(requested_ids)) == len(requested_ids)
                and len(results) == len(requested_ids)
            )
            if not valid:
                break
            for result in results:
                if (
                    type(result) is not dict
                    or set(result) != {"id", "replayed"}
                    or type(result.get("id")) is not str
                    or not result["id"]
                    or type(result.get("replayed")) is not bool
                ):
                    valid = False
                    break
                result_ids.append(result["id"])
            if not valid or (
                len(set(result_ids)) != len(result_ids)
                or set(result_ids) != set(requested_ids)
            ):
                valid = False
                break
    if not valid:
        raise ProtocolError(status_code) from None


_ADAPTER_DEADLINE = ContextVar("openeight_http_deadline", default=None)


class Client:
    """Machine-authenticated Connect client with explicit identity and owned cleanup."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str,
        agent_name: str,
        integration: AgentIntegration | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        normalized_url = _validate_base_url(base_url)
        self._api_key = _validate_api_key(api_key)
        self._agent_name = _validate_agent_name(agent_name)
        if integration is not None and integration not in (
            "otel",
            "langchain",
            "deepagents",
            "other",
        ):
            raise _configuration_error("invalid_agent_integration")
        self._integration = integration
        self._sdk_version = resolve_sdk_version()
        normalized_timeout = validate_timeout(timeout)
        client = None
        failure: tuple[Any, ...] | None = None
        try:
            client = httpx.Client(
                base_url=normalized_url,
                timeout=normalized_timeout,
                transport=transport,
                trust_env=False,
            )
        except httpx.InvalidURL:
            failure = (ConfigurationError, "invalid_base_url")
        except Exception:
            # Setup failures must not expose provider details or credentials.
            failure = (TransportError,)
        if failure is not None:
            del (
                self,
                base_url,
                api_key,
                agent_name,
                integration,
                timeout,
                transport,
                normalized_url,
                normalized_timeout,
            )
            raise _fresh_safe_error(failure) from None
        self._http = client
        self._adapter_http = None
        self._resource_lock = threading.RLock()
        self._close_condition = threading.Condition(self._resource_lock)
        self._owned_runs: dict[int, Any] = {}
        self._direct_identities: dict[str, Any] = {}
        self._direct_completion_attempted: set[str] = set()
        self._direct_starting: set[str] = set()
        self._direct_settlements: dict[str, threading.Event] = {}
        self._direct_completed: set[str] = set()
        self._owned_writers: dict[int, BufferedObserveWriter] = {}
        self._active_requests = 0
        self._transport_closing = False
        self._closing = False
        self._closed = False
        self._close_done = False
        self._close_error: tuple[Any, ...] | None = None

    @classmethod
    def from_env(
        cls,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> "Client":
        """Read endpoint, API key and Agent name from the SDK environment variables."""
        return cls(
            os.environ.get(BASE_URL_ENV),
            api_key=os.environ.get(API_KEY_ENV),
            agent_name=os.environ.get(AGENT_NAME_ENV),
            timeout=timeout,
            transport=transport,
        )

    def _ensure_open(self) -> None:
        with self._resource_lock:
            if self._closing or self._closed:
                raise _configuration_error("client_closed")

    def _register_owned_run(self, run: Any) -> None:
        with self._resource_lock:
            if self._closing or self._closed:
                raise _configuration_error("client_closed")
            self._owned_runs[id(run)] = run

    def _release_owned_run(self, run: Any) -> None:
        with self._resource_lock:
            self._owned_runs.pop(id(run), None)
            if run._completion_acknowledged:
                self._direct_completed.add(run.run_id)
            self._direct_identities.pop(run.run_id, None)
            self._direct_settlements.pop(run.run_id, None)

    def _register_owned_writer(self, writer: BufferedObserveWriter) -> None:
        with self._resource_lock:
            if self._closing or self._closed:
                raise _configuration_error("client_closed")
            writer._bind_owner(self._release_owned_writer)
            self._owned_writers[id(writer)] = writer

    def _release_owned_writer(self, writer: BufferedObserveWriter) -> None:
        with self._resource_lock:
            self._owned_writers.pop(id(writer), None)

    def _machine_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"ApiKey {self._api_key}",
            AGENT_NAME_HEADER: self._agent_name,
            **(
                {"X-OpenEight-Agent-Integration": self._integration}
                if self._integration is not None
                else {}
            ),
        }

    @staticmethod
    def _response_json(response: httpx.Response) -> tuple[Any, bool]:
        malformed = False
        value: Any = None
        try:
            value = response.json()
        except Exception:
            malformed = True
        return value, malformed

    @staticmethod
    def _rest_error(response: httpx.Response, data: Any) -> APIError | None:
        if not isinstance(data, dict):
            return None
        category = data.get("error")
        code = data.get("code")
        message = data.get("message")
        if (
            not isinstance(category, str)
            or category not in _REST_CATEGORIES
            or not isinstance(code, str)
            or _REST_REASON_PATTERN.fullmatch(code) is None
            or not isinstance(message, str)
        ):
            return None
        definition = _REST_ERROR_DEFINITIONS.get(code)
        safe_code = code if definition == (response.status_code, category) else None
        return APIError(response.status_code, category, safe_code)

    @staticmethod
    def _connect_error(response: httpx.Response, data: Any) -> APIError | None:
        if not isinstance(data, dict):
            return None
        code = data.get("code")
        message = data.get("message")
        if (
            not isinstance(code, str)
            or code not in _CONNECT_CODES
            or not isinstance(message, str)
        ):
            return None
        return APIError(
            response.status_code,
            code,
            governance_reason(response.status_code, code, data.get("details")) or code,
        )

    @contextmanager
    def _admit_request(self, allow_closing: bool):
        with self._close_condition:
            if (
                self._closed
                or self._transport_closing
                or (self._closing and not allow_closing)
            ):
                raise _configuration_error("client_closed")
            self._active_requests += 1
        try:
            yield
        finally:
            with self._close_condition:
                self._active_requests -= 1
                self._close_condition.notify_all()

    def _send(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool,
        protocol: str,
        json_body: Any = None,
        encoded_body: bytes | None = None,
        timeout: float | None = None,
        include_status: bool = False,
        allow_closing: bool = False,
    ) -> Any:
        with self._admit_request(allow_closing):
            with self._resource_lock:
                http = self._http
                unavailable = http is None or self._closed
                request_headers = self._machine_headers() if authenticated else {}
            if unavailable:
                raise TransportError() from None
            request_kwargs: dict[str, Any] = {"headers": request_headers}
            if encoded_body is not None:
                request_headers["Content-Type"] = "application/json"
                request_kwargs["content"] = encoded_body
            elif json_body is not None:
                request_kwargs["json"] = json_body
            bounded_timeout: float | None = None
            if timeout is not None:
                bounded_timeout = validate_timeout(timeout)
                request_kwargs["timeout"] = bounded_timeout
            deadline = (
                _ADAPTER_DEADLINE.get()
                if getattr(self, "_adapter_owned_send", False)
                else None
            )
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TransportError() from None
                bounded_timeout = min(bounded_timeout or 30.0, remaining)
                request_kwargs["timeout"] = bounded_timeout
            request: httpx.Request | None = None
            response: httpx.Response | None = None
            transport_failed = False
            try:
                request = http.build_request(method, path, **request_kwargs)
                if getattr(self, "_adapter_owned_send", False):
                    from openeight._adapter_http import DeadlineHTTPTransport

                    with self._resource_lock:
                        if self._http is None:
                            raise TransportError()
                        if self._adapter_http is None:
                            self._adapter_http = DeadlineHTTPTransport()
                        adapter_http = self._adapter_http
                    total_deadline = time.monotonic() + (
                        bounded_timeout or http.timeout.read
                    )
                    if deadline is not None:
                        total_deadline = min(total_deadline, deadline)
                    response = adapter_http.send(request, total_deadline)
                elif bounded_timeout is None:
                    response = http.send(request)
                else:
                    settled = threading.Event()
                    if path.endswith("/WriteTelemetry"):
                        payload = json.loads(request.content)
                        with self._resource_lock:
                            for row in payload.get("runs", []):
                                if row["id"] in self._direct_identities:
                                    self._direct_settlements[row["id"]] = settled
                    response, transport_failed = _send_sync_request_with_timeout(
                        http,
                        request,
                        bounded_timeout,
                        settled,
                    )
            except Exception:
                transport_failed = True
            if transport_failed:
                raise TransportError() from None
            if response is None:
                raise TransportError() from None

            data, malformed = self._response_json(response)
            if malformed:
                raise ProtocolError(response.status_code) from None
            if not 200 <= response.status_code < 300:
                error = (
                    self._rest_error(response, data)
                    if protocol == "rest"
                    else self._connect_error(response, data)
                    or self._rest_error(response, data)
                )
                if error is None:
                    raise ProtocolError(response.status_code) from None
                retry_after = response.headers.get("retry-after")
                try:
                    from email.utils import parsedate_to_datetime

                    delay = (
                        float(retry_after)
                        if retry_after is not None and retry_after.strip().isdigit()
                        else (
                            parsedate_to_datetime(retry_after)
                            - datetime.now(timezone.utc)
                        ).total_seconds()
                    )
                    if 0 <= delay <= 30:
                        error._retry_after = delay
                except Exception:
                    pass
                raise error from None
            if not isinstance(data, dict):
                raise ProtocolError(response.status_code) from None
            if include_status:
                return data, response.status_code
            return data

    def health(self) -> dict[str, Any]:
        """Read server health without authentication."""
        failure: tuple[Any, ...] | None = None
        result: dict[str, Any] | None = None
        try:
            result = self._send(
                "GET",
                "/healthz",
                authenticated=False,
                protocol="rest",
            )
        except SDKError as error:
            failure = _safe_error_descriptor(error)
        if failure is not None:
            del self
            raise _fresh_safe_error(failure) from None
        return result if result is not None else {}

    @safe_direct_errors
    def decide(
        self,
        *,
        decision_id: str = "",
        run_id: str = "",
        workflow_id: str | None = None,
        workflow_version: str | None = None,
        tool_id: str,
        tool_version_id: str,
        input: Any,
        policy_contract_set_digest: str,
        selected_values: Sequence[dict[str, Any]],
        source_time: datetime | str | None = None,
        timeout: float | None = None,
    ) -> DecisionResult:
        """Request an authorization decision; this method never executes the tool."""
        from openeight.decisions import prepare_decision

        prepared = prepare_decision(
            decision_id=decision_id,
            run_id=_uuid7_or_new(run_id, "invalid_run_id"),
            workflow_id=workflow_id,
            workflow_version=workflow_version,
            tool_id=tool_id,
            tool_version_id=tool_version_id,
            input=input,
            policy_contract_set_digest=policy_contract_set_digest,
            selected_values=selected_values,
            source_time=source_time,
        )
        return self.decide_prepared(prepared, timeout=timeout)

    @safe_direct_errors
    def decide_prepared(
        self, prepared: PreparedDecision, *, timeout: float | None = None
    ) -> DecisionResult:
        """Dispatch retained bytes under the same authenticated machine actor."""
        from openeight.decisions import PreparedDecision

        if not isinstance(prepared, PreparedDecision) or not hasattr(prepared, "_body"):
            raise ConfigurationError("invalid_input")
        failure = None
        result = None
        try:
            data, status = self._send(
                "POST",
                "/api/v1/openeight.govern.v1.GovernService/Decide",
                authenticated=True,
                protocol="connect",
                encoded_body=prepared._body,
                timeout=timeout,
                include_status=True,
            )
            try:
                result = validate_decision_result(data)
            except (TypeError, ValueError):
                raise ProtocolError(status) from None
            if (
                result.decision_id != prepared.decision_id
                or result.run_id != prepared.run_id
            ):
                raise ProtocolError(status) from None
        except SDKError as error:
            failure = _safe_error_descriptor(error)
        if failure is not None:
            del self, prepared
            raise _fresh_safe_error(failure) from None
        return result

    def register_tools(
        self, registrations: Sequence[ToolRegistration], *, timeout: float | None = None
    ) -> tuple[RegistrationResult, ...]:
        """Report tool facts and receive the matching pending or configured versions."""
        if (
            type(registrations) not in {list, tuple}
            or not 1 <= len(registrations) <= 200
            or not all(isinstance(item, ToolRegistration) for item in registrations)
        ):
            raise _configuration_error("invalid_tool_registrations")
        if len({item.name for item in registrations}) != len(registrations):
            raise _configuration_error("invalid_tool_registrations")
        body: dict[str, Any] = {
            "tools": [_serialize_tool_facts(item) for item in registrations]
        }
        if self._sdk_version is not None:
            body["sdk_version"] = self._sdk_version
        if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > 1048576:
            raise _configuration_error("invalid_tool_registrations")
        data, status = self._send(
            "POST",
            "/api/v1/openeight.observe.v1.ObserveService/RegisterTools",
            authenticated=True,
            protocol="connect",
            json_body=body,
            timeout=timeout,
            include_status=True,
        )
        try:
            results = data["results"]
            if type(results) is not list or len(results) != len(registrations):
                raise ValueError
            parsed = tuple(parse_registration(item) for item in results)
            for index, (request, result) in enumerate(zip(registrations, parsed)):
                if result.request_index != index:
                    raise ValueError
                if result.status in {"registered", "replayed"}:
                    tool, version, readiness = (
                        result.tool,
                        result.tool_version,
                        result.readiness,
                    )
                    if (
                        tool is None
                        or version is None
                        or readiness is None
                        or tool.name != request.name
                        or version.tool_id != tool.id
                        or tool.current_version_id not in (None, version.id)
                        or _serialize_tool_facts(request)["input_schema_json"]
                        != _serialize_tool_facts(
                            ToolRegistration(
                                name=tool.name,
                                description=tool.description,
                                input_schema=version.input_schema,
                            )
                        )["input_schema_json"]
                        or (
                            version.tool_contract_digest is not None
                            and not tool.governed
                        )
                        or readiness.name != tool.name
                        or readiness.tool_id != tool.id
                        or readiness.tool_version_id != version.id
                        or readiness.governed != tool.governed
                        or readiness.request_index not in (None, index)
                        or (
                            version.tool_contract_digest is None
                            and (readiness.eligible or readiness.policy_ready)
                            and not (
                                tool.capability == "read"
                                and not tool.governed
                                and readiness.reason == "read_only"
                                and readiness.eligible
                                and readiness.policy_ready
                            )
                        )
                    ):
                        raise ValueError
            return parsed
        except Exception:
            raise ProtocolError(status) from None

    def get_tool_readiness(
        self, tool_version_ids: Sequence[str], *, timeout: float | None = None
    ) -> AgentReadiness:
        """Read policy readiness for versions within the authenticated Agent catalog."""
        if type(tool_version_ids) not in {list, tuple} or len(tool_version_ids) > 200:
            raise _configuration_error("invalid_tool_version_ids")
        normalized = [
            _validate_uuid(item, "invalid_tool_version_ids")
            for item in tool_version_ids
        ]
        if len(set(normalized)) != len(normalized):
            raise _configuration_error("invalid_tool_version_ids")
        data, status = self._send(
            "POST",
            "/api/v1/openeight.observe.v1.ObserveService/GetToolReadiness",
            authenticated=True,
            protocol="connect",
            json_body={"tool_version_ids": normalized},
            timeout=timeout,
            include_status=True,
        )
        try:
            value = data["readiness"]
            if len(json.dumps(data, ensure_ascii=False).encode()) > 4 * 1024 * 1024:
                raise ValueError
            tools = tuple(parse_readiness(item) for item in value["tools"])
            if normalized and (
                len(tools) != len(normalized)
                or {tool.tool_version_id for tool in tools} != set(normalized)
            ):
                raise ValueError
            if (
                type(value["fully_registered"]) is not bool
                or type(value["fully_policy_ready"]) is not bool
            ):
                raise ValueError
            return AgentReadiness(
                tools,
                value["fully_registered"],
                value["registration_reason"],
                value["fully_policy_ready"],
                value["policy_reason"],
            )
        except Exception:
            raise ProtocolError(status) from None

    def approval_state(
        self,
        decision_id: str,
        *,
        timeout: float | None = None,
    ) -> str:
        """Read approval state without changing the stored Decision verdict."""
        normalized = _nonblank_string(decision_id, "invalid_decision_id")
        normalized = _uuid7_or_new(normalized, "invalid_decision_id")
        failure: tuple[Any, ...] | None = None
        state: Any = None
        try:
            data, status_code = self._send(
                "POST",
                "/api/v1/openeight.govern.v1.GovernService/GetApprovalState",
                authenticated=True,
                protocol="connect",
                json_body={"decision_id": normalized},
                timeout=timeout,
                include_status=True,
            )
            state = data.get("state")
            if state not in {"pending", "approved", "rejected", "expired"}:
                raise ProtocolError(status_code) from None
        except SDKError as error:
            failure = _safe_error_descriptor(error)
        if failure is not None:
            del self, decision_id, normalized, timeout
            if "data" in locals():
                del data
            raise _fresh_safe_error(failure) from None
        return state

    @safe_direct_errors
    def write_telemetry(
        self,
        *,
        runs: list[dict[str, Any]] | None = None,
        events: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
    ) -> None:
        """Submit Run/Event writes with caller-owned correlation IDs."""
        self._ensure_open()
        body = {
            "runs": _validate_telemetry_items(runs, "invalid_runs"),
            "events": _validate_telemetry_items(events, "invalid_events"),
        }
        if (
            len(body["runs"]) + len(body["events"]) > 1000
            or len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())
            > 4 * 1024 * 1024
        ):
            raise ConfigurationError("invalid_telemetry_limit")
        failure: tuple[Any, ...] | None = None
        try:
            data, status_code = self._send(
                "POST",
                "/api/v1/openeight.observe.v1.ObserveService/WriteTelemetry",
                authenticated=True,
                protocol="connect",
                json_body=body,
                timeout=timeout,
                include_status=True,
            )
            _validate_telemetry_acknowledgement(data, body, status_code)
        except SDKError as error:
            failure = _safe_error_descriptor(error)
        if failure is not None:
            del self, runs, events, timeout, body
            if "data" in locals():
                del data
            raise _fresh_safe_error(failure) from None

    def _complete_governed_run(
        self,
        run_id: str,
        started_at: str,
        ended_at: str,
        timeout: float,
    ) -> None:
        body = {
            "runs": [
                {
                    "id": run_id,
                    "status": "completed",
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "attributes_json": "{}",
                }
            ],
            "events": [],
        }
        identity = self._direct_identities.get(run_id)
        if identity is not None:
            body["runs"][0].update(identity._write())
            body["runs"][0].update(status="completed", ended_at=ended_at)
        data, status_code = self._send(
            "POST",
            "/api/v1/openeight.observe.v1.ObserveService/WriteTelemetry",
            authenticated=True,
            protocol="connect",
            json_body=body,
            timeout=timeout,
            include_status=True,
            allow_closing=True,
        )
        _validate_telemetry_acknowledgement(data, body, status_code)

    def _write_terminal_replay(
        self,
        *,
        run_id: str,
        started_at: datetime | str,
        ended_at: datetime | str,
        event_id: str,
        event: Event,
    ) -> None:
        failure: tuple[Any, ...] | None = None
        body: dict[str, Any] | None = None
        data: Any = None
        event_write: dict[str, Any] | None = None
        operation: Any = None
        normalized_event_id: str | None = None
        normalized_event_occurred_at: str | None = None
        normalized_run_id: str | None = None
        try:
            self._ensure_open()
            normalized_run_id = _uuid7_required(run_id, "invalid_run_id")
            normalized_event_id = _uuid7_required(event_id, "invalid_event")
            normalized_started_at = _normalize_source_timestamp(started_at)
            normalized_ended_at = _normalize_source_timestamp(ended_at)
            if not isinstance(event, Event):
                raise _configuration_error("invalid_event_type") from None
            normalized_event_occurred_at = _normalize_source_timestamp(
                event.occurred_at
            )
            ordered_times = tuple(
                datetime.fromisoformat(value.replace("Z", "+00:00"))
                for value in (
                    normalized_started_at,
                    normalized_event_occurred_at,
                    normalized_ended_at,
                )
            )
            if ordered_times != tuple(sorted(ordered_times)):
                raise _configuration_error("invalid_source_time") from None
            operation = _terminal_replay_operation(
                run_id=normalized_run_id,
                event_id=normalized_event_id,
                completed_at=normalized_ended_at,
                event=event._as_write(),
            )
            event_write = operation.event
            event_write["id"] = event_write.pop("event_id")
            event_write["run_id"] = normalized_run_id
            body = {
                "runs": [
                    {
                        "id": normalized_run_id,
                        "status": "completed",
                        "started_at": normalized_started_at,
                        "ended_at": operation.completed_at,
                        "attributes_json": "{}",
                    }
                ],
                "events": [event_write],
            }
            data, status_code = self._send(
                "POST",
                "/api/v1/openeight.observe.v1.ObserveService/WriteTelemetry",
                authenticated=True,
                protocol="connect",
                json_body=body,
                include_status=True,
            )
            _validate_telemetry_acknowledgement(data, body, status_code)
        except BaseException as error:
            failure = _safe_error_descriptor(error)
        if failure is not None:
            del (
                self,
                run_id,
                started_at,
                ended_at,
                event_id,
                event,
                body,
                data,
                event_write,
                operation,
                normalized_event_id,
                normalized_event_occurred_at,
                normalized_run_id,
            )
            raise _fresh_safe_error(failure) from None

    def _new_buffered_observe(
        self,
        *,
        owned: bool,
        _initial_run: tuple[str, str] | None = None,
        **kwargs: Any,
    ) -> BufferedObserveWriter:
        runs: dict[str, dict[str, Any]] = {}
        if _initial_run is not None:
            run_id, started_at = _initial_run
            runs[run_id] = (
                self._direct_identities[run_id]._write()
                if run_id in self._direct_identities
                else {
                    "id": run_id,
                    "status": "active",
                    "started_at": started_at,
                    "attributes_json": "{}",
                }
            )
        path = "/api/v1/openeight.observe.v1.ObserveService/WriteTelemetry"

        def event_writes(
            run_id: str, events: list[dict[str, Any]]
        ) -> list[dict[str, Any]]:
            writes = []
            for event in events:
                write = dict(event)
                write["id"] = write.pop("event_id")
                write["run_id"] = run_id
                writes.append(write)
            return writes

        def run_write(run_id: str) -> dict[str, Any]:
            return runs.setdefault(
                run_id,
                {
                    "id": run_id,
                    "status": "active",
                    "started_at": utc_timestamp(),
                    "attributes_json": "{}",
                },
            )

        def request_body(
            run_id: str,
            events: list[dict[str, Any]],
            ended_at: str | None,
        ) -> dict[str, Any]:
            run = run_write(run_id)
            if ended_at is not None:
                run = {**run, "status": "completed", "ended_at": ended_at}
            return {"runs": [run], "events": event_writes(run_id, events)}

        def encode_request(
            run_id: str,
            events: list[dict[str, Any]],
            ended_at: str | None,
        ) -> bytes:
            return json.dumps(
                request_body(run_id, events, ended_at),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()

        def deliver(
            run_id: str,
            events: list[dict[str, Any]],
            ended_at: str | None,
            timeout: float,
        ) -> dict[str, Any]:
            body = request_body(run_id, events, ended_at)
            result, status_code = self._send(
                "POST",
                path,
                authenticated=True,
                protocol="connect",
                encoded_body=json.dumps(
                    body,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode(),
                timeout=timeout,
                include_status=True,
                allow_closing=True,
            )
            result = _normalize_connect_response(result)
            _validate_telemetry_acknowledgement(result, body, status_code)
            if ended_at is not None:
                runs.pop(run_id, None)
            return result

        def send(
            run_id: str, events: list[dict[str, Any]], timeout: float
        ) -> dict[str, Any]:
            return deliver(run_id, events, None, timeout)

        def send_completed(
            run_id: str,
            events: list[dict[str, Any]],
            ended_at: str,
            timeout: float,
        ) -> dict[str, Any]:
            return deliver(run_id, events, ended_at, timeout)

        setattr(send, "_openeight_bounded", True)
        setattr(send_completed, "_openeight_bounded", True)
        writer = BufferedObserveWriter(
            send,
            send_completed=send_completed,
            encode_request=encode_request,
            **kwargs,
        )
        if owned:
            try:
                self._register_owned_writer(writer)
            except BaseException:
                writer.close()
                raise
        return writer

    def buffered_observe(
        self,
        *,
        max_records: int = 1000,
        max_bytes: int = 8 * 1024 * 1024,
        flush_records: int = 100,
        flush_bytes: int = 3 * 1024 * 1024,
        flush_interval: float = 1.0,
        max_request_records: int = 1000,
        max_request_bytes: int = 4 * 1024 * 1024,
    ) -> BufferedObserveWriter:
        """Create a client-owned telemetry buffer with bounded queue and request sizes.

        Flush and close report delivery failures; the timer retains failed writes
        for explicit retry. Client shutdown also closes this writer.
        """
        self._ensure_open()
        return self._new_buffered_observe(
            owned=True,
            max_records=max_records,
            max_bytes=max_bytes,
            flush_records=flush_records,
            flush_bytes=flush_bytes,
            flush_interval=flush_interval,
            max_request_records=max_request_records,
            max_request_bytes=max_request_bytes,
        )

    @safe_direct_errors
    def governed_run(
        self,
        *,
        identity: RunIdentity | None = None,
        run_id: str = "",
        started_at: datetime | str | None = None,
        attributes: Mapping[str, Any] | None = None,
        workflow_id: str | None = None,
        workflow_version: str | None = None,
    ) -> GovernedRun:
        """Acknowledge startup; retain the identity when its outcome is uncertain."""
        from openeight._direct import timestamp
        from openeight._json import snapshot_json
        from openeight.run import RunIdentity, prepare_run_identity

        if identity is None:
            identity = prepare_run_identity(
                run_id=run_id,
                started_at=started_at,
                attributes=attributes,
                workflow_id=workflow_id,
                workflow_version=workflow_version,
            )
        elif not isinstance(identity, RunIdentity):
            raise ConfigurationError("invalid_run")
        elif (
            run_id
            and run_id != identity.run_id
            or started_at is not None
            and timestamp(started_at) != identity.started_at
            or attributes is not None
            and snapshot_json(dict(attributes), "invalid_run") != identity._attributes
            or workflow_id is not None
            and workflow_id != identity.workflow_id
            or workflow_version is not None
            and workflow_version != identity.workflow_version
        ):
            raise ConfigurationError("invalid_run")
        return self._start_direct_run(identity)

    @safe_direct_errors
    def resume_governed_run(self, checkpoint: RunCheckpoint) -> GovernedRun:
        """Resume suspension facts under exclusive caller ownership and original scope."""
        from openeight.run import RunCheckpoint

        if not isinstance(checkpoint, RunCheckpoint):
            raise ConfigurationError("invalid_run")
        return self._start_direct_run(RunCheckpoint.from_dict(checkpoint.to_dict()))

    def _start_direct_run(self, identity: RunIdentity) -> GovernedRun:
        from openeight.run import GovernedRun

        with self._resource_lock:
            self._ensure_open()
            run_id = identity.run_id
            if (
                run_id in self._direct_starting
                or run_id in self._direct_completion_attempted
                or run_id in self._direct_completed
                or any(run.run_id == run_id for run in self._owned_runs.values())
            ):
                raise ConfigurationError("invalid_run")
            settlement = self._direct_settlements.get(run_id)
            if settlement is not None and not settlement.is_set():
                raise ConfigurationError("client_busy")
            previous = self._direct_identities.get(run_id)
            if previous is not None and previous._write() != identity._write():
                raise ConfigurationError("invalid_run")
            self._direct_starting.add(run_id)
            self._direct_identities[run_id] = identity
        writer = None
        try:
            writer = self._new_buffered_observe(
                owned=False, _initial_run=(run_id, identity.started_at)
            )
            handle = writer.begin_run(run_id)
            self.write_telemetry(runs=[identity._write()], events=[])
            governed = GovernedRun(self, writer, handle, identity.started_at)
            self._register_owned_run(governed)
            return governed
        except BaseException:
            if writer is not None:
                writer.close()
            raise
        finally:
            with self._resource_lock:
                self._direct_starting.discard(run_id)

    @staticmethod
    def _bounded_close(
        resource: Any,
    ) -> tuple[bool, tuple[Any, ...] | None]:
        done = threading.Event()
        failure: list[tuple[Any, ...]] = []

        def invoke() -> None:
            try:
                resource.close()
            except BaseException as error:
                failure.append(_safe_error_descriptor(error))
            finally:
                done.set()

        thread = threading.Thread(target=invoke, daemon=True)
        thread.start()
        completed = done.wait(_CLOSE_TIMEOUT_SECONDS)
        return completed, failure[0] if failure else None

    def _close_descriptor(self) -> tuple[Any, ...] | None:
        with self._close_condition:
            if self._closing:
                while not self._close_done:
                    self._close_condition.wait()
                return self._close_error
            if self._closed:
                return self._close_error
            self._closing = True
            runs = tuple(self._owned_runs.values())
            writers = tuple(self._owned_writers.values())

        descriptor: tuple[Any, ...] | None = None
        for resources in (runs, writers):
            for resource in resources:
                from openeight.run import GovernedRun

                if isinstance(resource, GovernedRun):
                    completed = True
                    resource_error = None
                    try:
                        resource._close_for_client_shutdown()
                    except BaseException as error:
                        resource_error = _safe_error_descriptor(error)
                elif isinstance(resource, BufferedObserveWriter):
                    completed = True
                    resource_error = None
                    try:
                        resource.close(timeout=_CLOSE_TIMEOUT_SECONDS)
                    except BaseException as error:
                        resource_error = _safe_error_descriptor(error)
                else:
                    completed, resource_error = self._bounded_close(resource)
                if descriptor is None:
                    if resource_error is not None:
                        descriptor = resource_error
                    elif not completed:
                        descriptor = _safe_error_descriptor(SDKError())

        with self._close_condition:
            self._transport_closing = True
            deadline = time.monotonic() + _CLOSE_TIMEOUT_SECONDS
            while self._active_requests:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if descriptor is None:
                        descriptor = _safe_error_descriptor(TransportError())
                    break
                self._close_condition.wait(remaining)
            http = self._http
            self._http = None
            adapter_http = self._adapter_http
            self._adapter_http = None
        if adapter_http is not None:
            try:
                adapter_http.close()
            except Exception:
                if descriptor is None:
                    descriptor = _safe_error_descriptor(TransportError())
        if http is not None:
            completed, close_error = self._bounded_close(http)
            if (not completed or close_error is not None) and descriptor is None:
                descriptor = _safe_error_descriptor(TransportError())
        with self._close_condition:
            self._closed = True
            self._close_done = True
            self._close_error = descriptor
            self._api_key = ""
            self._agent_name = ""
            self._owned_runs.clear()
            self._direct_identities.clear()
            self._direct_settlements.clear()
            self._owned_writers.clear()
            self._close_condition.notify_all()

        return descriptor

    def close(self) -> None:
        """Close owned Runs, buffered writers and transports, then clear credentials."""
        descriptor = self._close_descriptor()
        if descriptor is not None:
            del self
            raise _fresh_safe_error(descriptor) from None

    def __enter__(self) -> "Client":
        self._ensure_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.close()
        except SDKError:
            if exc is None:
                raise
