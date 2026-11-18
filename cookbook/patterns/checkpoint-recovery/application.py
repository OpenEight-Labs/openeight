"""Single-process SQLite sidecars; native SQLite/PostgreSQL saver remains separate."""

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict
from datetime import datetime, timedelta
from uuid import UUID

from openeight.checkpoints import (
    CheckpointBinding,
    CheckpointEnrollment,
    CheckpointExecution,
)

_HELD = ContextVar("ordinary_checkpoint_example_guard", default=None)


class ApplicationCheckpoints:
    """Own one instance and native saver per process; authorize is trusted application code."""

    def __init__(self, path, *, authorize):
        self.path, self.authorize = path, authorize
        self.guard = asyncio.Lock()
        self.generation = None
        with self.database() as database:
            database.executescript("""
                CREATE TABLE IF NOT EXISTS enrollment (
                    scope TEXT NOT NULL, thread TEXT NOT NULL, namespace TEXT NOT NULL,
                    body TEXT NOT NULL, PRIMARY KEY (scope, thread, namespace));
                CREATE TABLE IF NOT EXISTS executions (
                    id TEXT NOT NULL, scope TEXT NOT NULL, thread TEXT NOT NULL,
                    namespace TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY (scope, thread, namespace, id));
                CREATE INDEX IF NOT EXISTS execution_scope
                    ON executions(scope, thread, namespace);
            """)

    def database(self):
        return sqlite3.connect(self.path)

    @asynccontextmanager
    async def claim(self, *, config, context):
        # The callback authenticates context and authorizes exact selectors and mode.
        binding = await self.authorize(config=config, context=context)
        if type(binding) is not CheckpointBinding or self.guard.locked():
            raise PermissionError("checkpoint claim unavailable")
        await self.guard.acquire()
        generation = object()
        self.generation = generation
        token = _HELD.set(generation)
        claim = Claim(self, binding, generation)
        try:
            yield claim
        finally:
            self.generation = None
            _HELD.reset(token)
            self.guard.release()


class Claim:
    def __init__(self, access, binding, generation):
        self.access, self.binding, self.generation = access, binding, generation

    @property
    def key(self):
        return self.binding.scope_id, self.binding.thread_id, self.binding.checkpoint_ns

    async def assert_held(self):
        if (
            self.access.generation is not self.generation
            or _HELD.get() is not self.generation
        ):
            raise PermissionError("checkpoint claim is no longer held")

    async def read_binding(self):
        await self.assert_held()
        with self.access.database() as database:
            row = database.execute(
                "SELECT body FROM enrollment WHERE scope=? AND thread=? AND namespace=?",
                self.key,
            ).fetchone()
        return CheckpointEnrollment(**json.loads(row[0])) if row else None

    def check_record(self, value):
        if (value.scope_id, value.thread_id, value.checkpoint_ns) != self.key:
            raise PermissionError("checkpoint record scope mismatch")

    async def bind(self, value):
        await self.assert_held()
        self.check_record(value)
        prior = await self.read_binding()
        if prior is not None and prior != value:
            raise ValueError("immutable enrollment mismatch")
        with self.access.database() as database:
            database.execute(
                "INSERT OR IGNORE INTO enrollment VALUES (?,?,?,?)",
                (*self.key, json.dumps(asdict(value))),
            )
        if await self.read_binding() != value:
            raise ValueError("enrollment readback mismatch")

    async def last_execution(self):
        await self.assert_held()
        with self.access.database() as database:
            row = database.execute(
                "SELECT body FROM executions WHERE scope=? AND thread=? AND namespace=? ORDER BY rowid DESC LIMIT 1",
                self.key,
            ).fetchone()
        return CheckpointExecution(**json.loads(row[0])) if row else None

    def validate_execution(self, value, *, running):
        if type(value) is not CheckpointExecution:
            raise ValueError("invalid execution record")
        self.check_record(value)
        for identifier in (value.execution_id, value.run_id):
            if type(identifier) is not str or str(UUID(identifier)) != identifier:
                raise ValueError("invalid execution identity")
        for instant in (value.started_at, value.finished_at):
            if instant is not None and (
                type(instant) is not str
                or datetime.fromisoformat(instant).utcoffset() != timedelta(0)
            ):
                raise ValueError("invalid execution timestamp")
        if value.started_at is None or running != (value.finished_at is None):
            raise ValueError("invalid execution timestamps")
        for selector in (
            value.input_checkpoint_id,
            value.output_checkpoint_id,
            value.model_variant,
        ):
            if selector is not None and (type(selector) is not str or not selector):
                raise ValueError("invalid execution selector")
        if running:
            if value.state != "running" or value.output_checkpoint_id is not None:
                raise ValueError("invalid running execution")
        elif value.state not in {
            "settled",
            "native_interrupt",
            "recovery_required",
        } or (
            value.state != "recovery_required" and value.output_checkpoint_id is None
        ):
            raise ValueError("invalid terminal execution")

    async def begin(self, value):
        await self.assert_held()
        self.validate_execution(value, running=True)
        if await self.read_binding() is None:
            raise ValueError("enrollment is required")
        prior = await self.last_execution()
        if prior is not None and prior.state not in {"settled", "native_interrupt"}:
            raise ValueError("prior execution requires reconciliation")
        if value.state != "running":
            raise ValueError("execution must begin running")
        with self.access.database() as database:
            database.execute(
                "INSERT INTO executions VALUES (?,?,?,?,?)",
                (value.execution_id, *self.key, json.dumps(asdict(value))),
            )

    async def finish(self, value):
        await self.assert_held()
        self.validate_execution(value, running=False)
        prior = await self.last_execution()
        if prior == value:
            return
        if (
            prior is None
            or prior.state != "running"
            or any(
                getattr(prior, field) != getattr(value, field)
                for field in (
                    "scope_id",
                    "thread_id",
                    "checkpoint_ns",
                    "execution_id",
                    "run_id",
                    "started_at",
                    "input_checkpoint_id",
                    "model_variant",
                )
            )
        ):
            raise ValueError("execution compare and set failed")
        with self.access.database() as database:
            changed = database.execute(
                "UPDATE executions SET body=? WHERE scope=? AND thread=? AND namespace=? AND id=? AND body=?",
                (
                    json.dumps(asdict(value)),
                    *self.key,
                    value.execution_id,
                    json.dumps(asdict(prior)),
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("execution compare and set failed")
