"""Single-process application authority and persistent sidecars for saver qualification."""

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import asdict

from openeight.checkpoints import (
    CheckpointBinding,
    CheckpointEnrollment,
    CheckpointExecution,
)


class ApplicationCheckpoints:
    def __init__(self, path, *, scope_id="scope-a", graph_revision="release-1"):
        self.path, self.scope_id, self.graph_revision = path, scope_id, graph_revision
        self.guard = asyncio.Lock()
        self.authorized = True
        self.mode = "new"
        with self.database() as database:
            database.executescript("""
                CREATE TABLE IF NOT EXISTS enrollment (thread TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS executions (id TEXT PRIMARY KEY, thread TEXT NOT NULL, body TEXT NOT NULL);
            """)

    def database(self):
        return sqlite3.connect(self.path)

    @asynccontextmanager
    async def claim(self, *, config, context):
        if context is None or context.get("subject") != self.scope_id:
            raise PermissionError("application subject is unauthorized")
        thread = config.get("configurable", {}).get("thread_id")
        if thread != "application-thread":
            raise PermissionError("thread is outside application authority")
        async with self.guard:
            claim = Claim(
                self,
                CheckpointBinding(
                    scope_id=self.scope_id,
                    thread_id=thread,
                    checkpoint_ns="",
                    graph_revision=self.graph_revision,
                    model_variant=context.get("variant"),
                    mode=self.mode,
                ),
            )
            await claim.assert_held()
            yield claim


class Claim:
    def __init__(self, access, binding):
        self.access, self.binding = access, binding

    async def assert_held(self):
        if not self.access.guard.locked() or not self.access.authorized:
            raise PermissionError("application authority was revoked")

    async def read_binding(self):
        with self.access.database() as database:
            row = database.execute(
                "SELECT body FROM enrollment WHERE thread=?", (self.binding.thread_id,)
            ).fetchone()
        return CheckpointEnrollment(**json.loads(row[0])) if row else None

    async def bind(self, value):
        await self.assert_held()
        prior = await self.read_binding()
        if prior is not None and prior != value:
            raise ValueError("immutable enrollment mismatch")
        with self.access.database() as database:
            database.execute(
                "INSERT OR IGNORE INTO enrollment VALUES (?,?)",
                (self.binding.thread_id, json.dumps(asdict(value))),
            )

    async def last_execution(self):
        with self.access.database() as database:
            row = database.execute(
                "SELECT body FROM executions WHERE thread=? ORDER BY rowid DESC LIMIT 1",
                (self.binding.thread_id,),
            ).fetchone()
        return CheckpointExecution(**json.loads(row[0])) if row else None

    async def begin(self, value):
        await self.assert_held()
        prior = await self.last_execution()
        if prior is not None and prior.state not in {"settled", "native_interrupt"}:
            raise ValueError("prior execution is unresolved")
        with self.access.database() as database:
            database.execute(
                "INSERT INTO executions VALUES (?,?,?)",
                (value.execution_id, self.binding.thread_id, json.dumps(asdict(value))),
            )

    async def finish(self, value):
        await self.assert_held()
        prior = await self.last_execution()
        if (
            prior.execution_id != value.execution_id
            or prior.state != "running"
            and prior != value
        ):
            raise ValueError("execution compare and set failed")
        with self.access.database() as database:
            database.execute(
                "UPDATE executions SET body=? WHERE id=?",
                (json.dumps(asdict(value)), value.execution_id),
            )
