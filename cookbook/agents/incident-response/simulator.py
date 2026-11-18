"""Atomic, process-local synthetic checkout service. No recovery or network I/O."""

import json
from dataclasses import replace
from datetime import datetime, timezone
from threading import RLock
from uuid import uuid4

from contracts import (
    EffectRecord,
    Mutation,
    ReadResult,
    ServiceSnapshot,
)


class BusinessError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def strict_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = item
        return result

    def reject(_):
        raise ValueError("nonfinite")

    return json.loads(value, object_pairs_hook=pairs, parse_constant=reject)


def identifier(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid_identifier")


def validate_mutation(mutation):
    identifier(mutation.action_id)
    identifier(mutation.service_id)
    if type(mutation.expected_revision) is not int or mutation.expected_revision < 0:
        raise ValueError("invalid_revision")
    args = strict_json(mutation.arguments_json)
    fields = {
        "restart_replica": "replica_id",
        "rollback_release": "target_release",
        "set_replica_count": "replicas",
    }
    key = fields.get(mutation.operation)
    if not key or not isinstance(args, dict) or set(args) != {key}:
        raise ValueError("invalid_arguments")
    if mutation.operation == "set_replica_count":
        if type(args[key]) is not int or not 1 <= args[key] <= 10:
            raise BusinessError("invalid_replica_count")
    else:
        identifier(args[key])
    if canonical(args) != mutation.arguments_json:
        raise ValueError("noncanonical_arguments")
    return args


def initial_snapshot(service_id="checkout"):
    return ServiceSnapshot(
        service_id, "v42", 2, 0, (("replica-1", 0), ("replica-2", 0))
    )


def mutation_snapshot(s, known_releases, mutation):
    """Pure transition rules, shared with destination-history validation."""
    args = validate_mutation(mutation)
    if mutation.service_id != s.service_id:
        raise BusinessError("service_not_found")
    if mutation.expected_revision != s.revision:
        raise BusinessError("stale_revision")
    counters = dict(s.restart_counts)
    release, replicas = s.release, s.replicas
    if mutation.operation == "restart_replica":
        if args["replica_id"] not in counters:
            raise BusinessError("unknown_replica")
        counters[args["replica_id"]] += 1
    elif mutation.operation == "rollback_release":
        release = args["target_release"]
        if release not in known_releases:
            raise BusinessError("unknown_release")
    else:
        replicas = args["replicas"]
        counters = {
            f"replica-{n}": counters.get(f"replica-{n}", 0)
            for n in range(1, replicas + 1)
        }
    return ServiceSnapshot(
        s.service_id, release, replicas, s.revision + 1, tuple(counters.items())
    )


class IncidentSimulator:
    def __init__(self, *, initial: ServiceSnapshot, known_releases: tuple[str, ...]):
        identifier(initial.service_id)
        identifier(initial.release)
        if (
            type(initial.replicas) is not int
            or not 1 <= initial.replicas <= 10
            or type(initial.revision) is not int
            or initial.revision < 0
            or initial.release not in known_releases
            or tuple(key for key, _ in initial.restart_counts)
            != tuple(f"replica-{n}" for n in range(1, initial.replicas + 1))
            or any(type(v) is not int or v < 0 for _, v in initial.restart_counts)
        ):
            raise ValueError("invalid_snapshot")
        self._state = replace(
            initial,
            restart_counts=tuple(tuple(pair) for pair in initial.restart_counts),
        )
        self._releases = tuple(known_releases)
        self._effects = {}
        self._lock = RLock()

    def snapshot(self) -> ServiceSnapshot:
        with self._lock:
            return self._state

    def effects(self) -> tuple[EffectRecord, ...]:
        with self._lock:
            return tuple(self._effects.values())

    def _read(self, service_id, kind):
        with self._lock:
            self._service(service_id)
            s = self._state
            facts = {
                "logs": {"message": "checkout errors after v42", "service": service_id},
                "metrics": {
                    "error_rate": 0.18 if s.release == "v42" else 0.01,
                    "replicas": s.replicas,
                },
                "deployments": {"release": s.release, "known_releases": self._releases},
            }
            return ReadResult(service_id, s.revision, canonical(facts[kind]))

    def read_logs(self, *, service_id: str) -> ReadResult:
        return self._read(service_id, "logs")

    def read_metrics(self, *, service_id: str) -> ReadResult:
        return self._read(service_id, "metrics")

    def read_deployments(self, *, service_id: str) -> ReadResult:
        return self._read(service_id, "deployments")

    def _service(self, service_id):
        identifier(service_id)
        if service_id != self._state.service_id:
            raise BusinessError("service_not_found")

    def apply(self, mutation: Mutation) -> EffectRecord:
        with self._lock:
            validate_mutation(mutation)
            previous = self._effects.get(mutation.action_id)
            if previous:
                if previous.mutation != mutation:
                    raise BusinessError("action_conflict")
                return previous
            self._service(mutation.service_id)
            s = self._state
            if mutation.expected_revision != s.revision:
                raise BusinessError("stale_revision")
            updated = mutation_snapshot(s, self._releases, mutation)
            receipt = EffectRecord(
                str(uuid4()),
                mutation,
                s.revision,
                updated.revision,
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            self._state = updated
            self._effects[mutation.action_id] = receipt
            return receipt
