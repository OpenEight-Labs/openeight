"""Immutable, bounded telemetry submission helpers."""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from openeight._forbidden_fields import FORBIDDEN_TELEMETRY_SELECTORS
from openeight._time import utc_timestamp
from openeight._validation import validate_timeout
from openeight.errors import (
    ConfigurationError,
    ObserveDeliveryError,
    ObserveQueueFullError,
    ObserveRunCompletedError,
    SDKError,
    _fresh_safe_error,
    _safe_error_descriptor,
)
from openeight.types import _snapshot_event_json

ObserveSender = Callable[[str, list[dict[str, Any]], float], Any]
CompletedObserveSender = Callable[[str, list[dict[str, Any]], str, float], Any]

_WRITER_OWNED_EVENT_FIELDS = frozenset({"id", "event_id", "run_id"})


def _invoke_sender_until_deadline(
    callback: Callable[..., Any],
    arguments: tuple[Any, ...],
    deadline: float,
) -> str | None:
    """Wait for a caller's telemetry sender and return a safe delivery error code."""
    # A caller-supplied sender may ignore its timeout. Bound waiting without
    # implying that its external work was cancelled or acknowledged.
    done = threading.Event()
    failure: list[str] = []

    def invoke() -> None:
        try:
            callback(*arguments)
        except TimeoutError:
            failure.append("flush_timeout")
        except BaseException:
            failure.append("delivery_failed")
        finally:
            done.set()

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return "flush_timeout"
    threading.Thread(target=invoke, daemon=True).start()
    if not done.wait(remaining):
        return "flush_timeout"
    if time.monotonic() >= deadline:
        return "flush_timeout"
    return failure[0] if failure else None


def uuid7() -> str:
    """Return a UUIDv7 string on Python versions without ``uuid.uuid7``."""
    now_ms = int(time.time() * 1000)
    value = (now_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= (uuid.uuid4().int & ((1 << 62) - 1)) | (0b10 << 62)
    return str(uuid.UUID(int=value))


def _uuid7_run_id(value: str) -> str:
    parsed: uuid.UUID | None = None
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        pass
    if parsed is None or parsed.version != 7 or parsed.variant != uuid.RFC_4122:
        raise ConfigurationError("invalid_run_id") from None
    return str(parsed)


@dataclass(frozen=True)
class ObserveWriteOperation:
    run_id: str
    source_time: str
    event_id: str
    completed_at: str | None
    _event_bytes: bytes = field(repr=False)

    @property
    def event(self) -> dict[str, Any]:
        """Return a fresh copy of the accepted immutable Event snapshot."""
        return json.loads(self._event_bytes)


@dataclass(frozen=True)
class _TerminalReplayOperation:
    run_id: str
    event_id: str
    completed_at: str
    _event_bytes: bytes = field(repr=False)

    @property
    def event(self) -> dict[str, Any]:
        return json.loads(self._event_bytes)


@dataclass
class _ObserveRunState:
    run_id: str
    terminal: bool = False


def _canonical_json_bytes(value: Any) -> bytes:
    return _snapshot_event_json(value)


def _terminal_replay_operation(
    *,
    run_id: str,
    event_id: str,
    completed_at: str,
    event: dict[str, Any],
) -> _TerminalReplayOperation:
    snapshot = dict(event)
    snapshot["event_id"] = event_id
    return _TerminalReplayOperation(
        run_id=run_id,
        event_id=event_id,
        completed_at=completed_at,
        _event_bytes=_canonical_json_bytes(snapshot),
    )


class ObserveRunHandle:
    """Single-use submission handle for one buffered Observe Run."""

    def __init__(
        self, writer: "BufferedObserveWriter", state: _ObserveRunState
    ) -> None:
        self._writer = writer
        self._state = state

    @property
    def run_id(self) -> str:
        """Return the immutable Run identifier."""
        return self._state.run_id

    def submit(
        self,
        event: dict[str, Any],
        *,
        completed: bool = False,
    ) -> ObserveWriteOperation:
        """Queue an immutable event; completed=True ends this writer's Run.

        Return the operation used for delivery tracking and reject further events
        after completion. Queue limits and delivery failures raise SDK errors.
        """
        descriptor: tuple[Any, ...] | None = None
        operation: ObserveWriteOperation | None = None
        try:
            operation = self._writer._submit(
                self._state,
                event,
                completed=completed,
            )
        except SDKError as error:
            descriptor = _safe_error_descriptor(error)
        if descriptor is not None:
            del self, event, completed
            raise _fresh_safe_error(descriptor) from None
        assert operation is not None
        return operation


class BufferedObserveWriter:
    """Buffer immutable Observe Run/Event writes and flush them explicitly."""

    def __init__(
        self,
        send: ObserveSender,
        *,
        send_completed: CompletedObserveSender | None = None,
        encode_request: Callable[[str, list[dict[str, Any]], str | None], bytes]
        | None = None,
        max_records: int = 1000,
        max_bytes: int = 8 * 1024 * 1024,
        flush_records: int = 100,
        flush_bytes: int = 3 * 1024 * 1024,
        flush_interval: float = 1.0,
        max_request_records: int = 1000,
        max_request_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        if not callable(send):
            raise ConfigurationError("invalid_observe_sender") from None
        if send_completed is not None and not callable(send_completed):
            raise ConfigurationError("invalid_completed_sender") from None
        if encode_request is not None and not callable(encode_request):
            raise ConfigurationError("invalid_observe_encoder") from None
        integer_limits = (
            max_records,
            max_bytes,
            flush_records,
            flush_bytes,
            max_request_records,
            max_request_bytes,
        )
        if any(type(value) is not int or value <= 0 for value in integer_limits):
            raise ConfigurationError("invalid_writer_limit") from None
        if (
            isinstance(flush_interval, bool)
            or not isinstance(flush_interval, (int, float))
            or not math.isfinite(float(flush_interval))
            or flush_interval <= 0
            or max_request_records < 2
        ):
            raise ConfigurationError("invalid_writer_limit") from None
        if flush_records > max_records and flush_bytes > max_bytes:
            raise ConfigurationError("invalid_writer_threshold") from None
        self._send: ObserveSender | None = send
        self._send_completed = send_completed
        self._encode_request = encode_request or self._default_encode_request
        self._max_records = max_records
        self._max_bytes = max_bytes
        self._flush_records = flush_records
        self._flush_bytes = flush_bytes
        self._flush_interval = flush_interval
        self._max_request_records = max_request_records
        self._max_request_bytes = max_request_bytes
        self._pending: list[ObserveWriteOperation] = []
        self._failed: list[ObserveWriteOperation] = []
        self._records = 0
        self._bytes = 0
        self._pending_bytes = 0
        self._pending_since: float | None = None
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._closed = False
        self._closing = False
        self._close_done = False
        self._close_error: str | None = None
        self._close_condition = threading.Condition(self._lock)
        self._active_runs: dict[str, _ObserveRunState] = {}
        self._owner_release: Callable[["BufferedObserveWriter"], None] | None = None
        self._owner_released = False

    def _bind_owner(
        self,
        release: Callable[["BufferedObserveWriter"], None],
    ) -> None:
        with self._lock:
            self._owner_release = release

    def _release_owner(self) -> None:
        release = None
        with self._lock:
            if not self._owner_released:
                self._owner_released = True
                release = self._owner_release
                self._owner_release = None
        if release is not None:
            release(self)

    @staticmethod
    def _operation_size(operation: ObserveWriteOperation) -> int:
        return len(operation._event_bytes)

    @staticmethod
    def _default_encode_request(
        run_id: str,
        events: list[dict[str, Any]],
        completed_at: str | None,
    ) -> bytes:
        run: dict[str, Any] = {
            "id": run_id,
            "status": "completed" if completed_at is not None else "active",
        }
        if completed_at is not None:
            run["ended_at"] = completed_at
        event_writes = []
        for event in events:
            write = dict(event)
            write["id"] = write.pop("event_id")
            write["run_id"] = run_id
            event_writes.append(write)
        return json.dumps(
            {"runs": [run], "events": event_writes},
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()

    def _retain_undelivered(
        self,
        work: list[ObserveWriteOperation],
        delivered: int,
    ) -> None:
        with self._lock:
            self._failed.extend(work[delivered:])

    def begin_run(self, run_id: str | None = None) -> ObserveRunHandle:
        """Register one active UUIDv7 Run and return its submission handle."""
        normalized_run_id = _uuid7_run_id(uuid7() if run_id is None else run_id)
        with self._lock:
            if self._closed:
                raise ConfigurationError("writer_closed") from None
            if normalized_run_id in self._active_runs:
                raise ConfigurationError("run_already_registered") from None
            if len(self._active_runs) >= self._max_records:
                raise ObserveQueueFullError("run_registry_full") from None
            state = _ObserveRunState(normalized_run_id)
            self._active_runs[normalized_run_id] = state
        return ObserveRunHandle(self, state)

    def _submit(
        self,
        state: _ObserveRunState,
        event: dict[str, Any],
        *,
        completed: bool,
    ) -> ObserveWriteOperation:
        if type(completed) is not bool:
            raise ConfigurationError("invalid_event") from None
        frozen_event: dict[str, Any] | None = None
        if type(event) is dict:
            if not _WRITER_OWNED_EVENT_FIELDS.isdisjoint(event):
                raise ConfigurationError("invalid_event") from None
            if not FORBIDDEN_TELEMETRY_SELECTORS.isdisjoint(event):
                raise ConfigurationError("invalid_telemetry_scope") from None
            try:
                candidate = json.loads(_canonical_json_bytes(event))
                source_time = utc_timestamp()
                event_id = uuid7()
                candidate["event_id"] = event_id
                event_bytes = _canonical_json_bytes(candidate)
                frozen_event = candidate
            except Exception:
                pass
        if frozen_event is None:
            raise ConfigurationError("invalid_event") from None
        operation = ObserveWriteOperation(
            state.run_id,
            source_time,
            event_id,
            source_time if completed else None,
            event_bytes,
        )
        size = self._operation_size(operation)
        should_flush = False
        schedule_timer = False
        with self._lock:
            if self._closed:
                raise ConfigurationError("writer_closed") from None
            if state.terminal:
                raise ObserveRunCompletedError() from None
            if self._active_runs.get(state.run_id) is not state:
                raise ConfigurationError("run_not_registered") from None
            if (
                self._records >= self._max_records
                or self._bytes + size > self._max_bytes
            ):
                raise ObserveQueueFullError("write_queue_full") from None
            self._pending.append(operation)
            if completed:
                state.terminal = True
            self._records += 1
            self._bytes += size
            self._pending_bytes += size
            if self._pending_since is None:
                self._pending_since = time.monotonic()
                schedule_timer = True
            pending_age = time.monotonic() - self._pending_since
            should_flush = (
                completed
                or len(self._pending) >= self._flush_records
                or self._pending_bytes >= self._flush_bytes
                or pending_age >= self._flush_interval
            )
            if schedule_timer:
                self._timer = threading.Timer(self._flush_interval, self._timer_flush)
                self._timer.daemon = True
                self._timer.start()
        if should_flush:
            self.flush()
        return operation

    def _timer_flush(self) -> None:
        try:
            self.flush()
        except (ConfigurationError, ObserveDeliveryError):
            # The failed immutable operations remain queued for the caller's
            # explicit retry/close path to report.
            pass

    def flush(self, timeout: float = 10.0) -> None:
        """Deliver queued writes within timeout; retain failed writes for retry."""
        with self._lock:
            if self._closed:
                raise ConfigurationError("writer_closed") from None
        descriptor: tuple[Any, ...] | None = None
        try:
            self._flush_for_close(timeout)
        except SDKError as error:
            descriptor = _safe_error_descriptor(error)
        if descriptor is not None:
            del self, timeout
            raise _fresh_safe_error(descriptor) from None

    def _flush_for_close(self, timeout: float) -> None:
        deadline = time.monotonic() + validate_timeout(timeout)
        if not self._flush_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise ObserveDeliveryError("flush_timeout") from None
        try:
            self._flush_before(deadline)
        finally:
            self._flush_lock.release()

    @staticmethod
    def _completed_at(operations: list[ObserveWriteOperation]) -> str | None:
        return next(
            (
                operation.completed_at
                for operation in reversed(operations)
                if operation.completed_at is not None
            ),
            None,
        )

    def _request_bytes(self, operations: list[ObserveWriteOperation]) -> bytes:
        encoded = self._encode_request(
            operations[0].run_id,
            [operation.event for operation in operations],
            self._completed_at(operations),
        )
        if not isinstance(encoded, (bytes, bytearray)):
            raise TypeError("Observe request encoder must return bytes")
        return bytes(encoded)

    def _chunk_end(
        self,
        work: list[ObserveWriteOperation],
        start: int,
        segment_end: int,
    ) -> int:
        max_events = self._max_request_records - 1
        if max_events <= 0:
            return start
        upper = min(segment_end, start + max_events)
        if len(self._request_bytes(work[start:upper])) <= self._max_request_bytes:
            return upper
        if len(self._request_bytes(work[start : start + 1])) > self._max_request_bytes:
            return start

        low = start + 1
        high = upper
        while low < high:
            middle = (low + high + 1) // 2
            if len(self._request_bytes(work[start:middle])) <= self._max_request_bytes:
                low = middle
            else:
                high = middle - 1
        return low

    def _deliver_before(
        self,
        run_id: str,
        events: list[dict[str, Any]],
        completed_at: str | None,
        deadline: float,
    ) -> str | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "flush_timeout"
        if completed_at is not None and self._send_completed is not None:
            callback = self._send_completed
            arguments = (run_id, events, completed_at, remaining)
        else:
            callback = self._send
            arguments = (run_id, events, remaining)
        if callback is None:
            return "delivery_failed"
        if getattr(callback, "_openeight_bounded", False):
            try:
                callback(*arguments)
            except TimeoutError:
                return "flush_timeout"
            except BaseException:
                return (
                    "flush_timeout"
                    if time.monotonic() >= deadline
                    else "delivery_failed"
                )
            return "flush_timeout" if time.monotonic() >= deadline else None
        return _invoke_sender_until_deadline(callback, arguments, deadline)

    def _mark_delivered(
        self,
        run_id: str,
        chunk: list[ObserveWriteOperation],
        completed_at: str | None,
    ) -> None:
        with self._lock:
            self._records -= len(chunk)
            self._bytes -= sum(self._operation_size(operation) for operation in chunk)
            if completed_at is not None:
                state = self._active_runs.get(run_id)
                if state is not None and state.terminal:
                    self._active_runs.pop(run_id, None)

    def _flush_before(self, deadline: float) -> None:
        with self._lock:
            work = self._failed + self._pending
            self._failed = []
            self._pending = []
            self._pending_bytes = 0
            self._pending_since = None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        delivered = 0
        cursor = 0
        while cursor < len(work):
            run_id = work[cursor].run_id
            segment_end = cursor + 1
            while segment_end < len(work) and work[segment_end].run_id == run_id:
                segment_end += 1

            while cursor < segment_end:
                if time.monotonic() >= deadline:
                    self._retain_undelivered(work, delivered)
                    raise ObserveDeliveryError("flush_timeout") from None
                failure_reason = None
                try:
                    chunk_end = self._chunk_end(work, cursor, segment_end)
                except Exception:  # noqa: BLE001
                    failure_reason = (
                        "flush_timeout"
                        if time.monotonic() >= deadline
                        else "delivery_failed"
                    )
                if failure_reason is not None:
                    self._retain_undelivered(work, delivered)
                    raise ObserveDeliveryError(failure_reason) from None
                if chunk_end == cursor:
                    self._retain_undelivered(work, delivered)
                    raise ObserveDeliveryError("record_too_large") from None

                chunk = work[cursor:chunk_end]
                completed_at = self._completed_at(chunk)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._retain_undelivered(work, delivered)
                    raise ObserveDeliveryError("flush_timeout") from None
                failure_reason = self._deliver_before(
                    run_id,
                    [operation.event for operation in chunk],
                    completed_at,
                    deadline,
                )
                if failure_reason is not None:
                    self._retain_undelivered(work, delivered)
                    raise ObserveDeliveryError(failure_reason) from None
                if time.monotonic() >= deadline:
                    self._retain_undelivered(work, delivered)
                    raise ObserveDeliveryError("flush_timeout") from None

                self._mark_delivered(run_id, chunk, completed_at)
                delivered = chunk_end
                cursor = chunk_end

    def close(self, timeout: float = 10.0) -> None:
        """Stop accepting writes and drain the queue within timeout.

        Report delivery failures rather than silently discarding pending writes.
        """
        with self._close_condition:
            if self._closing:
                while not self._close_done:
                    self._close_condition.wait()
                close_error = self._close_error
                if close_error is not None:
                    del self, timeout
                    raise ObserveDeliveryError(close_error) from None
                return
            validate_timeout(timeout)
            self._closing = True
            self._closed = True

        close_error: str | None = None
        try:
            self._flush_for_close(timeout)
        except ObserveDeliveryError as exc:
            close_error = exc.reason
        finally:
            self._release_owner()
            with self._close_condition:
                self._close_error = close_error
                self._close_done = True
                self._pending = []
                self._failed = []
                self._records = 0
                self._bytes = 0
                self._pending_bytes = 0
                self._pending_since = None
                self._active_runs.clear()
                if self._timer is not None:
                    self._timer.cancel()
                    self._timer = None
                self._send = None
                self._send_completed = None
                self._encode_request = self._default_encode_request
                self._close_condition.notify_all()
        if close_error is not None:
            del self, timeout
            raise ObserveDeliveryError(close_error) from None

    def __enter__(self) -> "BufferedObserveWriter":
        with self._lock:
            if self._closed:
                raise ConfigurationError("writer_closed") from None
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
