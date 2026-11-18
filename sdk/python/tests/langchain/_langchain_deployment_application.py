"""Application-owned LC binding over the real reference-agent execution journal."""

import json
import sqlite3
import sys
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from uuid import uuid4

from _cookbook import recipe

from openeight import ConfigurationError
from openeight.langchain import DurableDeploymentEnrollment

sys.path.insert(0, str(recipe("agents/customer-support")))
from local import Deployment, now, SCHEMA_VERSION
from storage import SQLiteExecutionStore


class ExecutionStore(SQLiteExecutionStore):
    binding = None

    @contextmanager
    def _db(self):
        if self.binding is not None and self.binding.has_enrollment():
            self.binding.require_owned()
        with super()._db() as database:
            yield database


_GUARDS = {}


class ApplicationBinding:
    def __init__(self, path, store, *, revision="release-1"):
        self.path, self.execution_store, self.revision = path, store, revision
        self.deployment_id = store.deployment.id
        self.guard = _GUARDS.setdefault(str(path.resolve()), threading.Lock())
        self.held = ContextVar("application_deployment_ownership", default=None)
        self.active_token = None
        path.touch(mode=0o600, exist_ok=True)
        with sqlite3.connect(path) as database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS bindings (deployment_id TEXT PRIMARY KEY, body TEXT NOT NULL)"
            )
        store.binding = self

    def require_owned(self):
        if (
            self.active_token is None
            or self.held.get() is not self.active_token
            or not self.guard.locked()
        ):
            raise ConfigurationError("durable_deployment_mismatch")

    @contextmanager
    def acquire_deployment(self):
        if self.active_token is not None and self.held.get() is self.active_token:
            yield
            return
        if not self.guard.acquire(blocking=False):
            raise ConfigurationError("durable_deployment_busy")
        self.active_token = object()
        token = self.held.set(self.active_token)
        try:
            yield
        finally:
            self.active_token = None
            self.held.reset(token)
            self.guard.release()

    def has_enrollment(self):
        with sqlite3.connect(self.path) as database:
            return (
                database.execute("SELECT 1 FROM bindings LIMIT 1").fetchone()
                is not None
            )

    def deployment(self):
        self.require_owned()
        with sqlite3.connect(self.path) as database:
            row = database.execute(
                "SELECT body FROM bindings WHERE deployment_id=?", (self.deployment_id,)
            ).fetchone()
        return DurableDeploymentEnrollment(**json.loads(row[0])) if row else None

    def is_empty(self):
        self.require_owned()
        with self.execution_store._db() as database:
            return all(
                database.execute("SELECT 1 FROM " + table + " LIMIT 1").fetchone()
                is None
                for table in (
                    "threads",
                    "ui_commands",
                    "attempts",
                    "outbox",
                    "conversation_turns",
                    "local_attempts",
                    "catalog_versions",
                )
            )

    def bind_deployment(self, value):
        self.require_owned()
        existing = self.deployment()
        if existing is not None and existing != value:
            raise ConfigurationError("durable_deployment_mismatch")
        with sqlite3.connect(self.path) as database:
            database.execute(
                "INSERT OR IGNORE INTO bindings VALUES (?,?)",
                (value.deployment_id, json.dumps(asdict(value))),
            )


def application(directory):
    deployment = Deployment(
        str(uuid4()),
        "https://control.example",
        None,
        "support",
        None,
        None,
        "integrated",
        SCHEMA_VERSION,
        now(),
    )
    store = ExecutionStore(directory / "execution", deployment)
    store.initialize()
    return store, ApplicationBinding(directory / "binding.sqlite", store)
