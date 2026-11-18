"""One synchronous governed execution and its Observe Event correlation."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from openeight._direct import (
    identifier,
    safe_direct_errors,
    same_json,
    strict_object,
    timestamp,
    workflow,
)
from openeight._json import snapshot_json
from openeight._time import utc_timestamp
from openeight.errors import (
    ConfigurationError,
    ObserveDeliveryError,
    SDKError,
    _fresh_safe_error,
    _safe_error_descriptor,
)
from openeight.telemetry import BufferedObserveWriter, ObserveRunHandle, uuid7
from openeight.types import Event

_CLOSE_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, init=False, repr=False)
class RunIdentity:
    """Immutable startup facts, including the original attribute encoding."""

    run_id: str
    started_at: str
    _attributes: bytes
    attributes_json: str | None
    workflow_id: str | None
    workflow_version: str | None

    def __init__(self) -> None:
        raise ConfigurationError("invalid_run")

    @property
    def attributes(self) -> dict[str, Any]:
        return json.loads(self._attributes)

    def _write(self) -> dict[str, Any]:
        return {
            "id": self.run_id,
            "status": "active",
            "started_at": self.started_at,
            "attributes_json": self.attributes_json
            if self.attributes_json is not None
            else self._attributes.decode(),
            **workflow(self.workflow_id, self.workflow_version),
        }


def _identity(run_id, started_at, attributes, encoded, workflow_id, workflow_version):
    identifier(run_id, versions={7})
    started_at = timestamp(started_at)
    correlation = workflow(workflow_id, workflow_version)
    copied = snapshot_json(dict(attributes), "invalid_run", object_root=True)
    if encoded is not None and not same_json(
        strict_object(encoded), json.loads(copied)
    ):
        raise ConfigurationError("invalid_run")
    result = object.__new__(RunIdentity)
    for key, value in {
        "run_id": run_id,
        "started_at": started_at,
        "_attributes": copied,
        "attributes_json": encoded,
        "workflow_id": correlation.get("workflow_id"),
        "workflow_version": correlation.get("workflow_version"),
    }.items():
        object.__setattr__(result, key, value)
    return result


@safe_direct_errors
def prepare_run_identity(
    *,
    run_id: str = "",
    started_at: datetime | str | None = None,
    attributes: Mapping[str, Any] | None = None,
    workflow_id: str | None = None,
    workflow_version: str | None = None,
) -> RunIdentity:
    """Allocate and snapshot startup facts before any network request."""
    if (
        not isinstance(run_id, str)
        or attributes is not None
        and not isinstance(attributes, Mapping)
    ):
        raise ConfigurationError("invalid_run")
    attrs = dict(attributes) if attributes is not None else {}
    encoded = snapshot_json(attrs, "invalid_run", object_root=True).decode()
    return _identity(
        run_id or uuid7(),
        datetime.now(timezone.utc) if started_at is None else started_at,
        attrs,
        encoded,
        workflow_id,
        workflow_version,
    )


@dataclass(frozen=True, init=False, repr=False)
class RunCheckpoint(RunIdentity):
    """Suspension facts without credentials, pending Events or ownership fencing."""

    version = 1
    state = "suspended"

    def to_dict(self) -> dict[str, Any]:
        result = {
            "version": 1,
            "state": "suspended",
            "run_id": self.run_id,
            "started_at": self.started_at,
            "attributes": self.attributes,
            **workflow(self.workflow_id, self.workflow_version),
        }
        if self.attributes_json is not None:
            result["attributes_json"] = self.attributes_json
        return result

    @classmethod
    @safe_direct_errors
    def from_dict(cls, value: Mapping[str, Any]) -> RunCheckpoint:
        allowed = {
            "version",
            "state",
            "run_id",
            "started_at",
            "attributes",
            "attributes_json",
            "workflow_id",
            "workflow_version",
        }
        if (
            not isinstance(value, Mapping)
            or set(value) - allowed
            or type(value.get("version")) is not int
            or value.get("version") != 1
            or value.get("state") != "suspended"
            or type(value.get("attributes")) is not dict
        ):
            raise ConfigurationError("invalid_run")
        if any(
            key in value and not isinstance(value[key], str)
            for key in ("workflow_id", "workflow_version")
        ):
            raise ConfigurationError("invalid_run")
        if "attributes_json" in value and not isinstance(value["attributes_json"], str):
            raise ConfigurationError("invalid_run")
        if any(
            key in value and not isinstance(value[key], str)
            for key in ("workflow_id", "workflow_version")
        ):
            raise ConfigurationError("invalid_run")
        try:
            identity = _identity(
                value["run_id"],
                value["started_at"],
                value["attributes"],
                value.get("attributes_json"),
                value.get("workflow_id"),
                value.get("workflow_version"),
            )
        except (KeyError, TypeError, ValueError):
            raise ConfigurationError("invalid_run") from None
        checkpoint = object.__new__(cls)
        for key, item in vars(identity).items():
            object.__setattr__(checkpoint, key, item)
        return checkpoint


def _complete_run_with_timeout(
    client: Any,
    run_id: str,
    started_at: str,
    ended_at: str,
) -> tuple[Any, ...] | None:
    try:
        client._complete_governed_run(
            run_id,
            started_at,
            ended_at,
            _CLOSE_TIMEOUT_SECONDS,
        )
    except BaseException as error:
        return _safe_error_descriptor(error)
    return None


class GovernedRun:
    """Own one Run and its buffered events until completion or suspension."""

    def __init__(
        self,
        client: Any,
        writer: BufferedObserveWriter,
        handle: ObserveRunHandle,
        started_at: str,
    ) -> None:
        self._client = client
        self._writer = writer
        self._handle = handle
        self._run_id = handle.run_id
        self._started_at = started_at
        self._identity = client._direct_identities.get(self._run_id)
        self._ended_at: str | None = None
        self._completion_acknowledged = False
        self._state = "live"
        self._transition_kind: bool | None = None
        self._transition = 0
        self._completed_transition = 0
        self._transition_error: tuple[Any, ...] | None = None
        self._transition_waiters = 0
        self._terminal_error: tuple[Any, ...] | None = None
        self._shutdown_requested = False
        self._close_condition = threading.Condition()

    @property
    def run_id(self) -> str:
        """Return the immutable Run identifier."""
        return self._run_id

    @safe_direct_errors
    def submit_event(self, event: Event) -> None:
        """Queue an event for this live Run; reject writes after it closes."""
        with self._close_condition:
            if self._state != "live":
                raise ConfigurationError("run_closed") from None
            if not isinstance(event, Event):
                raise ConfigurationError("invalid_event_type") from None
            from openeight._telemetry_validation import validate_event

            write = event._as_write()
            validate_event(write, require_identity=False)
            self._handle.submit(write, completed=False)

    def _flush_live(self) -> None:
        with self._close_condition:
            if self._state != "live":
                raise ConfigurationError("run_closed") from None
            writer = self._writer
            descriptor: tuple[Any, ...] | None = None
            try:
                writer.flush(timeout=_CLOSE_TIMEOUT_SECONDS)
            except BaseException as error:
                descriptor = _safe_error_descriptor(error)

        if descriptor is not None:
            del self, writer
            raise _fresh_safe_error(descriptor) from None

    def _finish_complete(
        self,
        client: Any,
        writer: BufferedObserveWriter,
    ) -> tuple[Any, ...] | None:
        settlement = client._direct_settlements.get(self.run_id)
        if settlement is not None and not settlement.is_set():
            return (ConfigurationError, "client_busy")
        if not self._completion_acknowledged:
            try:
                writer.flush(timeout=_CLOSE_TIMEOUT_SECONDS)
            except BaseException as error:
                return _safe_error_descriptor(error)
        if not self._completion_acknowledged:
            completion_error = _complete_run_with_timeout(
                client,
                self.run_id,
                self._started_at,
                self._ended_at,
            )
            if completion_error is not None:
                return completion_error
            self._completion_acknowledged = True
        try:
            writer.close(timeout=_CLOSE_TIMEOUT_SECONDS)
        except BaseException as error:
            return _safe_error_descriptor(error)
        return None

    @staticmethod
    def _finish_suspend(
        writer: BufferedObserveWriter,
    ) -> tuple[tuple[Any, ...] | None, str]:
        try:
            writer.flush(timeout=_CLOSE_TIMEOUT_SECONDS)
        except BaseException:
            return (ObserveDeliveryError, "suspend_flush_failed"), "live"

        try:
            writer.close(timeout=_CLOSE_TIMEOUT_SECONDS)
        except BaseException:
            return (
                ObserveDeliveryError,
                "suspend_fresh_delivery_required",
            ), "inert"
        return None, "suspended"

    def _finish(self, *, complete: bool) -> None:
        descriptor: tuple[Any, ...] | None = None
        owns_transition = False
        with self._close_condition:
            while self._transition_waiters and self._state != "finishing":
                self._close_condition.wait()
            if self._state == "finishing":
                if self._transition_kind != complete and not self._shutdown_requested:
                    raise ConfigurationError("run_closed")
                transition = self._transition
                self._transition_waiters += 1
                while self._completed_transition < transition:
                    self._close_condition.wait()
                descriptor = self._transition_error
                self._transition_waiters -= 1
                if not self._transition_waiters:
                    self._close_condition.notify_all()
            elif (
                self._state in {"closed", "inert"} and self._terminal_error is not None
            ):
                descriptor = self._terminal_error
            elif self._state in {"closed", "suspended"}:
                if not complete and self._state == "closed":
                    raise ConfigurationError("run_closed")
                return
            elif not complete and self._shutdown_requested:
                descriptor = (ConfigurationError, "client_closed")
            elif not complete and self._state != "live":
                descriptor = (ConfigurationError, "run_closed")
            else:
                if complete and self._ended_at is None:
                    self._ended_at = max(
                        timestamp(utc_timestamp()), timestamp(self._started_at)
                    )
                    client = self._client
                    client._direct_completion_attempted.add(self.run_id)
                self._state = "finishing"
                self._transition_kind = complete
                self._transition += 1
                transition = self._transition
                client = self._client
                writer = self._writer
                owns_transition = True

        if descriptor is not None:
            del self, complete
            raise _fresh_safe_error(descriptor) from None
        if not owns_transition:
            return

        if complete:
            descriptor = self._finish_complete(client, writer)
            final_state = (
                "closed"
                if descriptor is None or self._completion_acknowledged
                else "completion_pending"
            )
            release = final_state == "closed"
        else:
            descriptor, final_state = self._finish_suspend(writer)
            release = final_state != "live"

        if release:
            client._release_owned_run(self)
        with self._close_condition:
            self._state = final_state
            self._completed_transition = transition
            self._transition_error = descriptor
            if final_state in {"closed", "inert"}:
                self._terminal_error = descriptor
            if release:
                self._client = None
                self._writer = None
                self._handle = None
            self._close_condition.notify_all()

        if descriptor is not None:
            del self, client, writer, complete, final_state, release
            raise _fresh_safe_error(descriptor) from None

    @safe_direct_errors
    def close(self) -> None:
        """Complete the Run and flush pending writes; report delivery failures."""
        descriptor: tuple[Any, ...] | None = None
        try:
            self._finish(complete=True)
        except SDKError as error:
            descriptor = _safe_error_descriptor(error)
        if descriptor is not None:
            del self
            raise _fresh_safe_error(descriptor) from None

    def _close_for_client_shutdown(self) -> None:
        with self._close_condition:
            # Shutdown owns the next transition after any in-flight winner settles.
            self._shutdown_requested = True

        descriptor = None
        while True:
            try:
                self.close()
                descriptor = None
            except SDKError as error:
                descriptor = _safe_error_descriptor(error)
            with self._close_condition:
                if self._state != "live":
                    break
        # Client shutdown is terminal, unlike a caller retrying Run.close().
        writer = self._writer
        if writer is not None:
            try:
                writer.close(timeout=_CLOSE_TIMEOUT_SECONDS)
            except BaseException as error:
                if descriptor is None:
                    descriptor = _safe_error_descriptor(error)
            with self._close_condition:
                self._state = "closed"
                self._terminal_error = descriptor
                self._client = self._writer = self._handle = None
        if descriptor is not None:
            del self, writer
            raise _fresh_safe_error(descriptor) from None

    def flush(self) -> None:
        """Flush pending writes without completing this live Run."""
        descriptor: tuple[Any, ...] | None = None
        try:
            self._flush_live()
        except SDKError as error:
            descriptor = _safe_error_descriptor(error)
        if descriptor is not None:
            del self
            raise _fresh_safe_error(descriptor) from None

    @safe_direct_errors
    def suspend(self) -> RunCheckpoint:
        """Flush and release this handle without marking the Run complete."""
        descriptor: tuple[Any, ...] | None = None
        try:
            self._finish(complete=False)
        except SDKError as error:
            descriptor = _safe_error_descriptor(error)
        if descriptor is not None:
            del self
            raise _fresh_safe_error(descriptor) from None
        return RunCheckpoint.from_dict(
            {
                "version": 1,
                "state": "suspended",
                "run_id": self._identity.run_id,
                "started_at": self._identity.started_at,
                "attributes": self._identity.attributes,
                **(
                    {"attributes_json": self._identity.attributes_json}
                    if self._identity.attributes_json is not None
                    else {}
                ),
                **workflow(self._identity.workflow_id, self._identity.workflow_version),
            }
        )

    def __enter__(self) -> "GovernedRun":
        with self._close_condition:
            if self._state != "live":
                raise ConfigurationError("run_closed") from None
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _traceback: Any,
    ) -> None:
        try:
            self.close()
        except SDKError:
            if exc is None:
                raise
