"""Application-owned synthetic service and atomic destination receipts."""

import hashlib
import os
import shutil
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote
from uuid import RFC_4122, UUID

from contracts import EffectRecord, Mutation, ServiceSnapshot
from simulator import (
    BusinessError,
    IncidentSimulator,
    canonical,
    initial_snapshot,
    mutation_snapshot,
    strict_json,
    validate_mutation,
)

APPLICATION_ID = 0x4F45494E
SCHEMA = """
CREATE TABLE configuration (
 id INTEGER PRIMARY KEY CHECK(id=1), format_version INTEGER NOT NULL CHECK(format_version=1),
 scenario TEXT NOT NULL CHECK(scenario IN ('investigate','restart','rollback','scale')),
 revision TEXT NOT NULL CHECK(length(CAST(revision AS BLOB)) BETWEEN 1 AND 256),
 known_releases_json TEXT NOT NULL CHECK(json_valid(known_releases_json)),
 initial_snapshot_json TEXT NOT NULL CHECK(json_valid(initial_snapshot_json)), created_at TEXT NOT NULL
);
CREATE TABLE service_state (
 id INTEGER PRIMARY KEY CHECK(id=1), snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json))
);
CREATE TABLE effect_receipts (
 idempotency_key TEXT PRIMARY KEY, action_id TEXT NOT NULL UNIQUE, effect_id TEXT NOT NULL UNIQUE,
 mutation_json TEXT NOT NULL CHECK(json_valid(mutation_json) AND length(CAST(mutation_json AS BLOB))<=65536),
 receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json) AND length(CAST(receipt_json AS BLOB))<=65536)
);
CREATE TABLE report_candidates (
 content_sha256 TEXT PRIMARY KEY CHECK(length(content_sha256)=64),
 content_json TEXT NOT NULL CHECK(json_valid(content_json) AND length(CAST(content_json AS BLOB))<=65536)
);
"""


def _schema(db):
    return tuple(
        tuple(row)
        for row in db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
        )
    )


with sqlite3.connect(":memory:") as _schema_db:
    _schema_db.executescript(SCHEMA)
    EXPECTED_SCHEMA = _schema(_schema_db)
_schema_db.close()


def _json(raw):
    value = strict_json(raw)
    if canonical(value) != raw or len(raw.encode()) > 65536:
        raise BusinessError("destination_inconsistent")
    return value


def _uuid(value):
    try:
        parsed = UUID(value)
        if not parsed.int or parsed.variant != RFC_4122 or str(parsed) != value:
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise BusinessError("invalid_idempotency_key") from None


def _file(path):
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise BusinessError("insecure_destination")


def _signature(path):
    result = []
    for suffix in ("", "-wal", "-shm", "-journal"):
        target = Path(str(path) + suffix)
        try:
            _file(target)
            info = target.lstat()
            result.append((suffix, info.st_ino, info.st_size, info.st_mtime_ns))
        except FileNotFoundError:
            result.append((suffix, None))
    return tuple(result)


def _inspect(path):
    before = _signature(path)
    rollback = Path(str(path) + "-journal")
    if rollback.exists() and rollback.stat().st_size:
        raise BusinessError("unsupported_destination")
    wal = Path(str(path) + "-wal")
    with tempfile.TemporaryDirectory(prefix="incident-header-") as temporary:
        target = path
        if wal.exists() and wal.stat().st_size:
            target = Path(temporary) / "incident.sqlite"
            for source, copied in ((path, target), (wal, Path(str(target) + "-wal"))):
                with source.open("rb") as incoming, copied.open("xb") as outgoing:
                    os.chmod(copied, 0o600)
                    shutil.copyfileobj(incoming, outgoing)
        if _signature(path) != before:
            raise BusinessError("destination_busy")
        uri = "file:" + quote(str(target), safe="/") + "?mode=ro"
        if target == path:
            uri += "&immutable=1"
        db = sqlite3.connect(uri, uri=True)
        try:
            schema = _schema(db)
            identity = (
                db.execute("PRAGMA application_id").fetchone()[0],
                db.execute("PRAGMA user_version").fetchone()[0],
            )
        finally:
            db.close()
        if _signature(path) != before:
            raise BusinessError("destination_busy")
        if not schema and identity == (0, 0):
            return False
        if schema != EXPECTED_SCHEMA or identity != (APPLICATION_ID, 1):
            raise BusinessError("unsupported_destination")
        return True


@contextmanager
def _transaction(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


class IncidentDestination:
    """One protected service destination, independent of SDK execution storage."""

    def __init__(self, directory, *, fault=None):
        self.directory = Path(os.path.abspath(os.fspath(directory)))
        self.path = self.directory / "incident.sqlite"
        self.fault = fault or (lambda _: None)

    def _directory(self, create):
        current = Path(self.directory.anchor)
        for part in self.directory.parts[1:]:
            current /= part
            if create and not current.exists() and not current.is_symlink():
                current.mkdir(mode=0o700)
            if not stat.S_ISDIR(current.lstat().st_mode):
                raise BusinessError("insecure_destination")
        info = self.directory.lstat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise BusinessError("insecure_destination")

    @contextmanager
    def _db(self, *, create=False):
        db = None
        try:
            self._directory(create)
            descriptor = os.open(
                self.path,
                os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0),
                0o600,
            )
            os.close(descriptor)
            _file(self.path)
            initialized = _inspect(self.path)
            if not initialized and not create:
                raise BusinessError("destination_uninitialized")
            db = sqlite3.connect(self.path, timeout=0.25, isolation_level=None)
            db.row_factory = sqlite3.Row
            if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            yield db, initialized
        except (OSError, sqlite3.Error):
            raise BusinessError("destination_unavailable") from None
        finally:
            if db is not None:
                db.close()

    def initialize(self, *, scenario, revision):
        from datetime import datetime, timezone

        if (
            scenario not in {"investigate", "restart", "rollback", "scale"}
            or type(revision) is not str
            or not revision.strip()
            or len(revision.encode()) > 256
        ):
            raise BusinessError("invalid_destination_configuration")
        expected = dict(
            scenario=scenario,
            revision=revision,
            known_releases_json=canonical(["v42", "v41"]),
            initial_snapshot_json=canonical(asdict(initial_snapshot())),
        )
        with self._db(create=True) as (db, initialized), _transaction(db):
            if not initialized:
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                db.execute("PRAGMA user_version=1")
                db.execute(
                    "INSERT INTO configuration VALUES (1,1,?,?,?,?,?)",
                    (
                        *expected.values(),
                        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    ),
                )
                db.execute(
                    "INSERT INTO service_state VALUES (1,?)",
                    (expected["initial_snapshot_json"],),
                )
            else:
                row = self._configuration(db)
                if any(row[key] != value for key, value in expected.items()):
                    raise BusinessError("destination_configuration_mismatch")

    def _configuration(self, db):
        row = db.execute("SELECT * FROM configuration WHERE id=1").fetchone()
        if row is None or row["format_version"] != 1:
            raise BusinessError("destination_inconsistent")
        return dict(row)

    def configuration(self):
        with self._db() as (db, _):
            return self._configuration(db)

    def _simulator(self, db, *, initial=False):
        config = self._configuration(db)
        row = db.execute(
            "SELECT snapshot_json FROM service_state WHERE id=1"
        ).fetchone()
        if row is None:
            raise BusinessError("destination_inconsistent")
        state = _json(
            config["initial_snapshot_json"] if initial else row["snapshot_json"]
        )
        state["restart_counts"] = tuple(tuple(pair) for pair in state["restart_counts"])
        return IncidentSimulator(
            initial=ServiceSnapshot(**state),
            known_releases=tuple(_json(config["known_releases_json"])),
        )

    def snapshot(self):
        with self._db() as (db, _):
            return self._simulator(db).snapshot()

    def initial_snapshot(self):
        with self._db() as (db, _):
            return self._simulator(db, initial=True).snapshot()

    def _receipt(self, row):
        from datetime import datetime, timedelta

        try:
            value = _json(row["receipt_json"])
            value["mutation"] = Mutation(**value["mutation"])
            validate_mutation(value["mutation"])
            receipt = EffectRecord(**value)
            stamp = datetime.fromisoformat(receipt.completed_at.replace("Z", "+00:00"))
            if (
                receipt.effect_id != row["effect_id"]
                or receipt.mutation.action_id != row["action_id"]
                or canonical(asdict(receipt.mutation)) != row["mutation_json"]
                or type(receipt.before_revision) is not int
                or type(receipt.after_revision) is not int
                or receipt.before_revision < 0
                or receipt.before_revision != receipt.mutation.expected_revision
                or receipt.after_revision != receipt.before_revision + 1
                or stamp.utcoffset() != timedelta(0)
                or stamp.isoformat().replace("+00:00", "Z") != receipt.completed_at
            ):
                raise ValueError
            _uuid(receipt.effect_id)
            _uuid(row["idempotency_key"])
            return receipt
        except (ValueError, TypeError, KeyError, AttributeError):
            raise BusinessError("destination_inconsistent") from None

    def _verified_effects(self, db):
        expected = self._simulator(db, initial=True).snapshot()
        releases = tuple(_json(self._configuration(db)["known_releases_json"]))
        rows = db.execute(
            "SELECT * FROM effect_receipts ORDER BY rowid LIMIT 65"
        ).fetchall()
        if len(rows) > 64:
            raise BusinessError("destination_limit")
        receipts = []
        try:
            for row in rows:
                receipt = self._receipt(row)
                expected = mutation_snapshot(expected, releases, receipt.mutation)
                receipts.append(receipt)
            if expected != self._simulator(db).snapshot():
                raise ValueError
        except (BusinessError, ValueError, TypeError, KeyError):
            raise BusinessError("destination_inconsistent") from None
        return tuple(receipts)

    def effects(self):
        with self._db() as (db, _):
            db.execute("BEGIN")
            return self._verified_effects(db)

    def lookup(self, key):
        _uuid(key)
        with self._db() as (db, _):
            db.execute("BEGIN")
            self._verified_effects(db)
            row = db.execute(
                "SELECT * FROM effect_receipts WHERE idempotency_key=?", (key,)
            ).fetchone()
            return self._receipt(row) if row else None

    def apply_keyed(self, mutation, *, idempotency_key):
        _uuid(idempotency_key)
        validate_mutation(mutation)
        raw = canonical(asdict(mutation))
        _json(raw)
        with self._db() as (db, _), _transaction(db):
            receipts = self._verified_effects(db)
            previous = db.execute(
                "SELECT * FROM effect_receipts WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if previous:
                receipt = self._receipt(previous)
                if receipt.mutation != mutation:
                    raise BusinessError("action_conflict")
                return receipt
            if db.execute(
                "SELECT 1 FROM effect_receipts WHERE action_id=?", (mutation.action_id,)
            ).fetchone():
                raise BusinessError("action_conflict")
            if len(receipts) >= 64:
                raise BusinessError("destination_limit")
            simulator = self._simulator(db)
            receipt = simulator.apply(mutation)
            encoded = canonical(asdict(receipt))
            _json(encoded)
            db.execute(
                "UPDATE service_state SET snapshot_json=? WHERE id=1",
                (canonical(asdict(simulator.snapshot())),),
            )
            self.fault("after_service_update")
            db.execute(
                "INSERT INTO effect_receipts VALUES (?,?,?,?,?)",
                (idempotency_key, mutation.action_id, receipt.effect_id, raw, encoded),
            )
            self.fault("after_destination_receipt")
        self.fault("after_destination_commit")
        return receipt

    def _read(self, service_id, name):
        with self._db() as (db, _):
            return getattr(self._simulator(db), name)(service_id=service_id)

    def read_logs(self, *, service_id):
        return self._read(service_id, "read_logs")

    def read_metrics(self, *, service_id):
        return self._read(service_id, "read_metrics")

    def read_deployments(self, *, service_id):
        return self._read(service_id, "read_deployments")

    def record_report(self, content):
        _json(content)
        digest = hashlib.sha256(content.encode()).hexdigest()
        with self._db() as (db, _), _transaction(db):
            row = db.execute(
                "SELECT content_json FROM report_candidates WHERE content_sha256=?",
                (digest,),
            ).fetchone()
            if row and row[0] != content:
                raise BusinessError("report_conflict")
            if (
                row is None
                and db.execute("SELECT count(*) FROM report_candidates").fetchone()[0]
                >= 64
            ):
                raise BusinessError("destination_limit")
            db.execute(
                "INSERT OR IGNORE INTO report_candidates VALUES (?,?)",
                (digest, content),
            )

    def has_report(self, content):
        _json(content)
        with self._db() as (db, _):
            row = db.execute(
                "SELECT content_json FROM report_candidates WHERE content_sha256=?",
                (hashlib.sha256(content.encode()).hexdigest(),),
            ).fetchone()
            return bool(row and row[0] == content)
