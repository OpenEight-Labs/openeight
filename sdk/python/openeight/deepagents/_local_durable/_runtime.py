"""Open final-path local resources without enrolling or admitting execution."""

import os
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass

from openeight._managed_types import ManagedResources
from openeight.errors import ConfigurationError


def _read_marker(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            value = stream.read(257)
        if len(value) > 256:
            raise ConfigurationError("durable_deployment_mismatch")
        return value.decode("utf-8")
    except (OSError, UnicodeError):
        raise ConfigurationError("durable_deployment_mismatch") from None


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class LocalRuntime:
    store: object
    resources: ManagedResources

    def validate_ready(self):
        from .. import _deployment as model
        from ._filesystem import protected_file

        marker = self.store.state / "ready"
        if not marker.exists() and not marker.is_symlink():
            raise ConfigurationError("durable_deployment_unbound")
        protected_file(marker)
        raw = _read_marker(marker)
        value = model.parse(raw)
        deployment = self.store.deployment()
        if (
            deployment.manifest_json is None
            or value != {"format_version": 1, "deployment_id": deployment.deployment_id}
            or type(value.get("format_version")) is not int
        ):
            raise ConfigurationError("durable_deployment_mismatch")
        return deployment

    def publish_ready(self):
        from .. import _deployment as model
        from ._filesystem import protected_file

        self.store._owned()
        deployment = self.store.deployment()
        if deployment.manifest_json is None:
            raise ConfigurationError("durable_deployment_unbound")
        raw = model.canonical(
            {"format_version": 1, "deployment_id": deployment.deployment_id}
        )
        marker, pending = (
            self.store.state / "ready",
            self.store.state / ".ready-pending",
        )
        if marker.exists() or marker.is_symlink():
            self.validate_ready()
            try:
                if pending.exists() or pending.is_symlink():
                    protected_file(pending)
                    if _read_marker(pending) != raw:
                        raise ConfigurationError("durable_deployment_mismatch")
                    pending.unlink()
                _sync_directory(self.store.state)
            except OSError:
                raise ConfigurationError("durable_persistence_failed") from None
            return
        with self.store._db() as db:
            self.store._empty(db)
        try:
            if pending.exists() or pending.is_symlink():
                protected_file(pending)
                previous = _read_marker(pending)
                # Only an interrupted prefix of this exact immutable marker can recover.
                if not raw.startswith(previous):
                    raise ConfigurationError("durable_deployment_mismatch")
                fd = os.open(pending, os.O_WRONLY | os.O_NOFOLLOW)
            else:
                fd = os.open(
                    pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                self.store.fault("ready_created")
                stream.write(raw)
                stream.flush()
                self.store.fault("ready_written")
                os.fsync(stream.fileno())
                self.store.fault("ready_synced")
            # Link never replaces an existing immutable marker, including a symlink.
            os.link(pending, marker, follow_symlinks=False)
            self.store.fault("ready_linked")
            pending.unlink()
            _sync_directory(self.store.state)
        except (OSError, UnicodeError):
            raise ConfigurationError("durable_persistence_failed") from None
        self.validate_ready()


def _inspect_checkpoint_file(path, expected):
    from ._filesystem import LocalError, inspection_connection

    try:
        with inspection_connection(path) as db:
            schema = tuple(
                tuple(row)
                for row in db.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
                )
            )
            if schema and schema != expected:
                raise LocalError("unsupported_deployment")
            return bool(schema) and any(
                db.execute("SELECT 1 FROM " + table + " LIMIT 1").fetchone()
                for table in ("checkpoints", "writes")
            )
    except sqlite3.Error:
        raise LocalError("unsupported_deployment") from None


def _enrolled_journal(directory):
    """Inspect schema/enrollment headers without initializing or opening a writable store."""
    from ._filesystem import inspection_connection
    from ._store import SQLiteDeepAgentsStore

    path = directory / "execution.sqlite"
    if not path.exists() and not path.is_symlink():
        return False
    inspection = SQLiteDeepAgentsStore.__new__(SQLiteDeepAgentsStore)
    inspection.path = path
    inspection._inspect_existing()
    try:
        with inspection_connection(path) as db:
            tables = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='deployments'"
            ).fetchone()
            if not tables:
                return False
            if not db.execute("SELECT 1 FROM deployments LIMIT 1").fetchone():
                inspection._empty(db)
                return False
            deployment = inspection._read_deployment(db)
            if deployment.manifest_json is None:
                inspection._empty(db)
                return False
            return True
    except sqlite3.Error:
        raise ConfigurationError("durable_deployment_mismatch") from None


@asynccontextmanager
async def open_local_durability(config):
    if os.name != "posix":
        raise ConfigurationError("unsupported_execution_method")
    try:
        import aiosqlite
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    except ImportError:
        raise ConfigurationError("durable_dependencies_missing") from None
    from ..durable import LocalDurability
    from ._filesystem import LocalError, protected_directory, protected_file
    from ._store import SQLiteDeepAgentsStore

    if not isinstance(config, LocalDurability):
        raise ConfigurationError("invalid_integration_configuration")
    directory = protected_directory(config.directory)
    allowed = {".locks", "ready", ".ready-pending"} | {
        filename + suffix
        for filename in ("execution.sqlite", "checkpoints.sqlite")
        for suffix in ("", "-wal", "-shm", "-journal")
    }
    if any(path.name not in allowed for path in directory.iterdir()):
        raise LocalError("unsupported_deployment")
    # Derive the exact native schema from the qualified saver, without touching user data.
    async with aiosqlite.connect(":memory:") as memory:
        reference = AsyncSqliteSaver(
            memory, serde=JsonPlusSerializer(pickle_fallback=False)
        )
        await reference.setup()
        async with memory.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
        ) as cursor:
            expected = tuple(await cursor.fetchall())
    checkpoint_file = directory / "checkpoints.sqlite"
    for filename in ("ready", ".ready-pending"):
        marker = directory / filename
        if marker.exists() or marker.is_symlink():
            protected_file(marker)
            if (
                not (directory / "execution.sqlite").exists()
                or not checkpoint_file.exists()
            ):
                raise ConfigurationError("durable_deployment_mismatch")
    enrolled = _enrolled_journal(directory)
    if checkpoint_file.exists() or checkpoint_file.is_symlink():
        populated = _inspect_checkpoint_file(checkpoint_file, expected)
        if populated and not enrolled:
            raise ConfigurationError("durable_deployment_mismatch")
    store = SQLiteDeepAgentsStore(directory)
    if store._deployment_id is not None and not checkpoint_file.exists():
        raise ConfigurationError("durable_checkpoint_missing")
    with store.acquire_deployment():
        protected_file(checkpoint_file, create=True)
        _inspect_checkpoint_file(checkpoint_file, expected)
        connection = await aiosqlite.connect(checkpoint_file)
        try:
            await connection.execute("PRAGMA synchronous=FULL")
            saver = AsyncSqliteSaver(
                connection, serde=JsonPlusSerializer(pickle_fallback=False)
            )
            await saver.setup()
        except BaseException:
            await connection.close()
            raise
    runtime = LocalRuntime(store, ManagedResources(checkpointer=saver))
    try:
        if (directory / "ready").exists() or (directory / "ready").is_symlink():
            runtime.validate_ready()
        yield runtime
    finally:
        await connection.close()


def compose_local_governance(governance, config, runtime):
    from dataclasses import replace

    from openeight.langchain.durable import DurableApprovalConfig, ReconciliationResult

    async def unknown(_):
        return ReconciliationResult("unknown")

    async def terminal(_):
        return "terminal"

    return replace(
        governance,
        durable=DurableApprovalConfig(
            store=runtime.store,
            reconcile=config.reconcile or unknown,
            classify_turn=config.classify_turn or terminal,
            max_model_calls=config.max_model_calls,
            max_tool_attempts=config.max_tool_attempts,
        ),
    )
