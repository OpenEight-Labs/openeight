"""Bounded per-owner delivery with independent catalog and telemetry jobs."""

import json
import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass

from ._json import thaw_json
from .delivery import DeliveryStatus
from .errors import APIError, ConfigurationError, TransportError


@dataclass
class _DeliveryJob:
    """One queued payload and its acknowledgement dependencies."""

    sequence: int
    kind: str
    run_id: str
    payload: object
    size: int
    after: tuple
    acknowledged: bool = False
    failed: bool = False
    next_at: float = 0
    retries: int = 0
    catalog_rejected: bool = False
    settle: object = None


class DeliveryWorker:
    """Deliver catalog and telemetry jobs within one owner’s record/byte budget."""

    def __init__(self, client, config, completeness, diagnostic):
        self.client, self.config = client, config
        self.catalog_publisher = client
        self.completeness, self.diagnostic = completeness, diagnostic
        self.condition = threading.Condition(threading.RLock())
        self.jobs = deque()
        self.sequence = self.bytes = self.reserved_records = self.reserved_bytes = 0
        self.dropped = self.failed = 0
        self.last_error = None
        self.closed = self.stopping = False
        self.thread = None
        self.inflight = ()
        self.pid = os.getpid()
        self.catalog_cache_bytes = 0
        self.last_kind = None

    def _ensure_process(self):
        if os.getpid() != self.pid:
            raise ConfigurationError("instrumentation_process_changed")

    def set_catalog_cache(self, size):
        """Account retained catalog projections without counting them as pending work."""
        self._ensure_process()
        with self.condition:
            if size + self.bytes + self.reserved_bytes > self.config.max_bytes:
                self.last_error = "catalog_cache_full"
                return False
            self.catalog_cache_bytes = size
            return True

    def reserve(self, size):
        """Reserve capacity for in-process callback or execution state."""
        self._ensure_process()
        with self.condition:
            if (
                self.closed
                or self.stopping
                or len(self.jobs) + self.reserved_records >= self.config.max_records
                or self.bytes + self.reserved_bytes + self.catalog_cache_bytes + size
                > self.config.max_bytes
            ):
                self.dropped += 1
                self.last_error = "queue_full"
                return False
            self.reserved_records += 1
            self.reserved_bytes += size
            return True

    def release(self, size):
        """Return a previously reserved record and its byte charge."""
        self._ensure_process()
        with self.condition:
            self.reserved_records -= 1
            self.reserved_bytes -= size
            self.condition.notify_all()

    def enqueue(self, kind, run_id, payload, after=()):
        """Snapshot and queue a payload; dependencies must acknowledge first."""
        self._ensure_process()
        if kind == "catalog":
            size = sum(
                len(
                    json.dumps(
                        thaw_json(f.input_schema),
                        separators=(",", ":"),
                        sort_keys=True,
                        allow_nan=False,
                    ).encode()
                )
                + len(f.name.encode())
                + len(f.description.encode())
                + 256
                for f in payload[0]
            )
        else:
            raw = json.dumps(
                payload, separators=(",", ":"), sort_keys=True, allow_nan=False
            ).encode()
            payload = json.loads(raw)
            size = len(raw) + 256
        with self.condition:
            if not self.reserve(size):
                return None
            self.release(size)
            self.sequence += 1
            job = _DeliveryJob(self.sequence, kind, run_id, payload, size, tuple(after))
            if kind == "catalog":
                if len(payload) == 3:
                    job.settle = payload[2]
            self.jobs.append(job)
            self.bytes += size
            if self.thread is None:
                self.thread = threading.Thread(
                    target=self._run, daemon=True, name="openeight-delivery"
                )
                try:
                    self.thread.start()
                except BaseException:
                    self.thread = None
                    self._settle_job(job, dropped=True)
                    raise
            self.condition.notify_all()
            return job

    def _settle_job(self, job, failed=False, dropped=False):
        if job not in self.jobs:
            return
        self.jobs.remove(job)
        self.bytes -= job.size
        job.acknowledged = not failed and not dropped
        job.failed = failed or dropped
        job.payload = None
        job.after = ()
        if failed:
            self.failed += 1
        if dropped:
            self.dropped += 1
        self.condition.notify_all()

    def _run(self):
        while True:
            self._ensure_process()
            with self.condition:
                if self.stopping:
                    return
                now = time.monotonic()
                ready = None
                for job in tuple(self.jobs):
                    if any(
                        dependency is None or dependency.failed
                        for dependency in job.after
                    ):
                        self._settle_job(job, failed=True)
                        self.last_error = "dependency_failed"
                        continue
                    if job.next_at <= now and all(
                        dep.acknowledged for dep in job.after
                    ):
                        if ready is None or job.kind != "catalog":
                            ready = job
                        if self.last_kind != "catalog" or job.kind != "catalog":
                            break
                if ready is None:
                    self.condition.wait(min(self.config.flush_interval, 0.1))
                    continue
            with self.condition:
                if self.stopping:
                    return
                if ready not in self.jobs:
                    continue
                batch = [ready]
                if ready.kind != "catalog":
                    size = ready.size
                    for candidate in self.jobs:
                        if (
                            candidate is ready
                            or candidate.kind == "catalog"
                            or candidate.next_at > time.monotonic()
                            or not all(
                                dep is not None and dep.acknowledged
                                for dep in candidate.after
                            )
                        ):
                            continue
                        if len(
                            batch
                        ) >= self.config.flush_records or size + candidate.size > min(
                            self.config.flush_bytes, 4 * 1024 * 1024
                        ):
                            break
                        batch.append(candidate)
                        size += candidate.size
                self.inflight = tuple(batch)
            error = None
            try:
                self._ensure_process()
                if ready.kind == "catalog":
                    facts, publish = ready.payload[:2]
                    from .langchain._catalog import send_catalog_batch

                    result = send_catalog_batch(
                        self.catalog_publisher, facts, timeout=1.0
                    )
                    publish(result)
                    ready.catalog_rejected |= any(
                        item.status in {"invalid", "conflict"} for item in result
                    )
                else:
                    self.client.write_telemetry(
                        runs=[job.payload for job in batch if job.kind != "event"],
                        events=[job.payload for job in batch if job.kind == "event"],
                        timeout=1.0,
                    )
            except BaseException as failure:
                error = failure
            notifications = []
            with self.condition:
                self.last_kind = ready.kind
                for job in batch:
                    dropped = False
                    if job not in self.jobs:
                        continue
                    if error is None:
                        self._settle_job(job, failed=job.catalog_rejected)
                        if job.catalog_rejected:
                            self.last_error = "catalog_failed"
                    elif (
                        isinstance(error, TransportError)
                        or isinstance(error, APIError)
                        and (error.status_code == 429 or error.status_code >= 500)
                    ):
                        if self.stopping:
                            dropped = True
                            self._settle_job(job, dropped=True)
                        else:
                            job.retries += 1
                            job.next_at = time.monotonic() + max(
                                getattr(error, "_retry_after", 0),
                                min(30, 2 ** min(job.retries - 1, 5))
                                * random.uniform(0.9, 1.1),
                            )
                            self.last_error = "delivery_retry"
                    else:
                        self._settle_job(job, failed=True)
                        self.last_error = (
                            "catalog_failed"
                            if job.kind == "catalog"
                            else "delivery_failed"
                        )
                    if job.payload is None and job.settle is not None:
                        notifications.append((job.settle, job.failed, dropped))

            for settle, failed, dropped in notifications:
                settle(failed, dropped)
            with self.condition:
                self.inflight = ()
                self.condition.notify_all()

    def status(self):
        self._ensure_process()
        with self.condition:
            return DeliveryStatus(
                pending_records=len(self.jobs) + self.reserved_records,
                pending_bytes=self.bytes + self.reserved_bytes,
                dropped_records=self.dropped,
                failed_records=self.failed,
                last_error_code=self.last_error,
                closed=self.closed,
                discovery_completeness=self.completeness,
                discovery_diagnostic=self.diagnostic,
            )

    def flush(self, timeout=10.0):
        """Wait for the current enqueue sequence, without waiting for future traffic."""
        from ._validation import validate_timeout

        self._ensure_process()
        deadline = time.monotonic() + validate_timeout(timeout)
        with self.condition:
            sequence = self.sequence
            while any(job.sequence <= sequence for job in (*self.jobs, *self.inflight)):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.last_error = "flush_timeout"
                    break
                self.condition.wait(remaining)
        return self.status()

    def close(self, timeout=10.0):
        """Stop admission, drop queued jobs and wait for the in-flight request."""
        from ._validation import validate_timeout

        self._ensure_process()
        deadline = time.monotonic() + validate_timeout(timeout)
        notifications = []
        with self.condition:
            self.closed = True
        self.flush(max(0.001, deadline - time.monotonic()))
        with self.condition:
            self.stopping = True
            for job in tuple(self.jobs):
                if job not in self.inflight:
                    self._settle_job(job, dropped=True)
                    if job.settle is not None:
                        notifications.append(job.settle)
            self.condition.notify_all()
        for settle in notifications:
            settle(False, True)
        if self.thread is not None:
            self.thread.join(timeout=max(0, deadline - time.monotonic()))
        with self.condition:
            self.closed = self.thread is None or not self.thread.is_alive()
            if not self.closed:
                self.last_error = "flush_timeout"
        return self.status()
