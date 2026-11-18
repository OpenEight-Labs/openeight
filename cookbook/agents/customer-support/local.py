"""Protected local SQLite connections and stable, nonblocking POSIX locks."""

from contextlib import contextmanager
from dataclasses import dataclass, asdict
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
from datetime import datetime, timezone


SCHEMA_VERSION = 4


class LocalError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def now():
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class Deployment:
    id: str
    endpoint: str | None
    workspace_id: str | None
    agent_name: str | None
    agent_id: str | None
    api_key_id: str | None
    mode: str
    schema_version: int
    created_at: str

    def validate(self):
        if self.schema_version != SCHEMA_VERSION or self.mode not in {"standalone", "integrated"}:
            raise LocalError("unsupported_deployment")
        if not all(type(v) is str and v for v in (self.id, self.created_at)):
            raise LocalError("invalid_deployment")
        selectors = (
            self.endpoint,
            self.workspace_id,
            self.agent_name,
            self.agent_id,
            self.api_key_id,
        )
        if self.mode == "standalone":
            if any(v is not None for v in selectors):
                raise LocalError("invalid_deployment")
        elif not all(type(v) is str and v for v in (self.endpoint, self.agent_name)):
            raise LocalError("invalid_deployment")
        if any(v is not None and (type(v) is not str or not v) for v in selectors):
            raise LocalError("invalid_deployment")


DEPLOYMENT_SQL = """CREATE TABLE deployment (
 id TEXT PRIMARY KEY, endpoint TEXT, workspace_id TEXT, agent_name TEXT,
 agent_id TEXT, api_key_id TEXT, mode TEXT NOT NULL CHECK(mode IN ('standalone','integrated')),
 schema_version INTEGER NOT NULL CHECK(schema_version=4), created_at TEXT NOT NULL,
 CHECK((mode='standalone' AND endpoint IS NULL AND workspace_id IS NULL AND agent_name IS NULL AND agent_id IS NULL AND api_key_id IS NULL)
 OR (mode='integrated' AND length(endpoint)>0 AND length(agent_name)>0))
);"""


def protected_directory(path):
    path = Path(path)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    st = path.lstat()
    if (
        not stat.S_ISDIR(st.st_mode)
        or st.st_uid != os.getuid()
        or stat.S_IMODE(st.st_mode) != 0o700
    ):
        raise LocalError("insecure_state")
    return path


def protected_secret(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            st = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_uid != os.getuid()
                or stat.S_IMODE(st.st_mode) != 0o600
            ):
                raise LocalError("insecure_secret")
            value = stream.read(65537).strip()
        if not value or len(value.encode()) > 65536:
            raise LocalError("invalid_secret")
        return value
    except OSError:
        raise LocalError("unavailable_secret") from None


@contextmanager
def connection(path):
    db = None
    try:
        path = Path(path)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            st = os.fstat(fd)
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_uid != os.getuid()
                or stat.S_IMODE(st.st_mode) != 0o600
            ):
                raise LocalError("insecure_state")
        finally:
            os.close(fd)
        # Allow brief WAL recovery/metadata contention; file locks remain nonblocking.
        db = sqlite3.connect(path, timeout=0.25, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
            db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        yield db
    except sqlite3.Error:
        raise LocalError("storage_unavailable") from None
    finally:
        if db is not None:
            db.close()


@contextmanager
def transaction(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


class Locks:
    def __init__(self, state):
        state = Path(state)
        self.directory = protected_directory(state.parent / (state.name + ".locks"))

    @contextmanager
    def hold(self, name, *, exclusive=True):
        if not name or "/" in name or name in {".", ".."}:
            raise LocalError("invalid_lock")
        fd = os.open(
            self.directory / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            st = os.fstat(fd)
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_uid != os.getuid()
                or stat.S_IMODE(st.st_mode) != 0o600
            ):
                raise LocalError("insecure_state")
            try:
                fcntl.flock(
                    fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
                )
            except BlockingIOError:
                raise LocalError("execution_busy") from None
            yield
        finally:
            os.close(fd)

    @contextmanager
    def thread(self, thread_id):
        from uuid import UUID

        try:
            if str(UUID(thread_id)) != thread_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise LocalError("invalid_thread") from None
        with self.hold("lifecycle", exclusive=False), self.hold("thread-" + thread_id):
            yield


def install_deployment(db, deployment):
    deployment.validate()
    row = db.execute("SELECT * FROM deployment").fetchone()
    if row:
        if dict(row) != asdict(deployment):
            raise LocalError("deployment_mismatch")
    else:
        data = asdict(deployment)
        db.execute(
            "INSERT INTO deployment VALUES (?,?,?,?,?,?,?,?,?)", tuple(data.values())
        )


def read_deployment(db):
    rows = db.execute("SELECT * FROM deployment").fetchall()
    if len(rows) != 1:
        raise LocalError("deployment_mismatch")
    if rows[0]["schema_version"] != SCHEMA_VERSION:
        raise LocalError("unsupported_deployment")
    result = Deployment(**dict(rows[0]))
    result.validate()
    return result
