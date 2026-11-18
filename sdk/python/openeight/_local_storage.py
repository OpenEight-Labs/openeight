"""Protected local files and nonblocking ownership for the SDK journal."""

import fcntl
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from openeight.errors import ConfigurationError


class LocalError(ConfigurationError):
    def __init__(self, code):
        self.code = code
        reason = {
            "execution_busy": "integration_busy",
            "thread_busy": "integration_busy",
            "deployment_mismatch": "durable_deployment_mismatch",
            "unsupported_deployment": "durable_deployment_mismatch",
            "not_found": "durable_thread_missing",
            "attempt_conflict": "durable_attempt_conflict",
            "invalid_attempt": "durable_attempt_conflict",
            "command_conflict": "durable_command_conflict",
            "attempt_pending": "durable_reconciliation_required",
        }.get(code, "durable_persistence_failed")
        super().__init__(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def now():
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def protected_directory(value):
    path = Path(os.path.abspath(os.fspath(value)))
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current = current / part
            if not current.exists() and not current.is_symlink():
                current.mkdir(mode=0o700)
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise LocalError("insecure_state")
        info = path.lstat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise LocalError("insecure_state")
        return path
    except OSError:
        raise LocalError("insecure_state") from None


def _check_file(path):
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise LocalError("insecure_state")


def protected_file(path, *, create=False):
    try:
        flags = os.O_RDWR | os.O_NOFOLLOW
        if create:
            flags |= os.O_CREAT
        fd = os.open(path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise LocalError("insecure_state")
        finally:
            os.close(fd)
        return Path(path)
    except OSError:
        raise LocalError("insecure_state") from None


def check_sidecars(path):
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            _check_file(sidecar)


def _file_signature(path):
    values = []
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(path) + suffix)
        try:
            info = candidate.lstat()
            values.append(
                (
                    suffix,
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )
            )
        except FileNotFoundError:
            values.append((suffix, None))
    return tuple(values)


@contextmanager
def inspection_connection(path):
    """Inspect headers without SQLite creating sidecars beside untrusted state."""
    protected_file(path)
    check_sidecars(path)
    before = _file_signature(path)
    rollback = Path(str(path) + "-journal")
    if rollback.exists() and rollback.stat().st_size:
        raise LocalError("unsupported_deployment")
    wal = Path(str(path) + "-wal")
    temporary = None
    db = None
    try:
        if wal.exists() and wal.stat().st_size:
            # A crash can leave committed headers only in WAL. Inspect isolated bytes;
            # the execution saver still opens the original final path after admission.
            temporary = tempfile.TemporaryDirectory(prefix="openeight-header-")
            copied = Path(temporary.name) / "state.sqlite"
            for source, target in ((path, copied), (wal, Path(str(copied) + "-wal"))):
                with source.open("rb") as incoming, target.open("xb") as outgoing:
                    os.chmod(target, 0o600)
                    shutil.copyfileobj(incoming, outgoing)
            uri = "file:" + quote(str(copied), safe="/") + "?mode=ro"
        else:
            uri = "file:" + quote(str(path), safe="/") + "?mode=ro&immutable=1"
        if _file_signature(path) != before:
            raise LocalError("execution_busy")
        db = sqlite3.connect(uri, uri=True)
        db.row_factory = sqlite3.Row
        yield db
        if _file_signature(path) != before:
            raise LocalError("execution_busy")
    except (sqlite3.Error, OSError):
        raise LocalError("unsupported_deployment") from None
    finally:
        if db is not None:
            db.close()
        if temporary is not None:
            temporary.cleanup()


@contextmanager
def connection(path, *, create=False):
    db = None
    try:
        path = protected_file(path, create=create)
        check_sidecars(path)
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
    if db.in_transaction:
        # Only SDK-owned nested operations share an outer atomic receipt settlement.
        yield
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


class Locks:
    def __init__(self, directory):
        self.directory = protected_directory(directory / ".locks")

    @contextmanager
    def hold(self, name):
        if type(name) is not str or not name or "/" in name or name in {".", ".."}:
            raise LocalError("invalid_lock")
        path = protected_file(self.directory / name, create=True)
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LocalError("execution_busy") from None
            yield
        finally:
            os.close(fd)
