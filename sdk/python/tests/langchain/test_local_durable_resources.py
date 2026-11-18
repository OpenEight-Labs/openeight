"""Private final-path resource lifecycle; no authority or command admission."""

import os
import sqlite3

import pytest

pytest.importorskip("deepagents")
pytest.importorskip("aiosqlite")

from openeight import ConfigurationError
from openeight.deepagents._local_durable._runtime import open_local_durability
from openeight.deepagents.durable import LocalDurability


@pytest.mark.asyncio
async def test_local_entry_only_creates_protected_recognized_empty_resources(tmp_path):
    config = LocalDurability(directory=tmp_path, revision="release-1")
    async with open_local_durability(config) as runtime:
        saver = runtime.resources.checkpointer
        assert runtime.store._deployment_id is None
        assert not (tmp_path / "ready").exists()
        assert not (tmp_path / ".ready-pending").exists()
        with pytest.raises(ConfigurationError) as caught:
            runtime.validate_ready()
        assert caught.value.reason == "durable_deployment_unbound"
        for path in tmp_path.iterdir():
            assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)
        assert not [item async for item in saver.alist(None)]
    with pytest.raises(ValueError):
        await saver.conn.execute("SELECT 1")
    async with open_local_durability(config) as reopened:
        assert reopened.store._deployment_id is None
        assert reopened.resources.checkpointer is not saver


@pytest.mark.asyncio
async def test_foreign_checkpoint_schema_is_rejected_before_journal_creation(tmp_path):
    path = tmp_path / "checkpoints.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE foreign_data(value TEXT)")
        db.execute("INSERT INTO foreign_data VALUES ('preserve')")
    path.chmod(0o600)
    before = path.read_bytes()
    with pytest.raises(ConfigurationError):
        async with open_local_durability(
            LocalDurability(directory=tmp_path, revision="r1")
        ):
            pytest.fail("foreign schema must reject")
    assert path.read_bytes() == before
    assert not (tmp_path / "execution.sqlite").exists()
    assert not (tmp_path / ".locks").exists()


@pytest.mark.asyncio
async def test_unknown_directory_content_is_preserved_without_creating_databases(
    tmp_path,
):
    path = tmp_path / "application.db"
    path.write_text("preserve")
    with pytest.raises(ConfigurationError):
        async with open_local_durability(
            LocalDurability(directory=tmp_path, revision="r1")
        ):
            pytest.fail("foreign directory must reject")
    assert {p.name for p in tmp_path.iterdir()} == {"application.db"}
    assert path.read_text() == "preserve"


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["ready", ".ready-pending"])
async def test_ready_symlink_is_rejected_before_database_creation(tmp_path, filename):
    target = tmp_path.parent / (filename.replace(".", "") + "-foreign")
    target.write_text("preserve")
    target.chmod(0o600)
    (tmp_path / filename).symlink_to(target)
    with pytest.raises(ConfigurationError):
        async with open_local_durability(
            LocalDurability(directory=tmp_path, revision="r1")
        ):
            pytest.fail("symlink marker must reject")
    assert target.read_text() == "preserve"
    assert not (tmp_path / "execution.sqlite").exists()


@pytest.mark.asyncio
async def test_missing_checkpoint_file_in_enrolled_state_never_silently_recreates(
    tmp_path,
):
    from test_local_durable_store import IDENTITY, canonical

    config = LocalDurability(directory=tmp_path, revision="r1")
    async with open_local_durability(config) as runtime:
        with runtime.store.acquire_deployment():
            runtime.store.initialize_enrollment(canonical(IDENTITY), "a" * 64)
    os.unlink(tmp_path / "checkpoints.sqlite")
    with pytest.raises(ConfigurationError) as caught:
        async with open_local_durability(config):
            pytest.fail("lost native state must reject")
    assert caught.value.reason == "durable_checkpoint_missing"
    assert not (tmp_path / "checkpoints.sqlite").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True])
async def test_ready_publication_is_explicit_immutable_and_recovers_pending_marker(
    tmp_path, pending
):
    from test_local_durable_store import IDENTITY, canonical, manifest

    config = LocalDurability(directory=tmp_path, revision="r1")
    async with open_local_durability(config) as runtime:
        with runtime.store.acquire_deployment():
            deployment = runtime.store.initialize_enrollment(
                canonical(IDENTITY), "a" * 64
            )
            runtime.store.bind_deployment(
                deployment.deployment_id, manifest(deployment)
            )
            assert not (tmp_path / "ready").exists()
            if pending:
                path = tmp_path / ".ready-pending"
                path.write_text(
                    canonical(
                        {"format_version": 1, "deployment_id": deployment.deployment_id}
                    )
                )
                path.chmod(0o600)
            runtime.publish_ready()
            before = (tmp_path / "ready").stat()
            runtime.publish_ready()
            assert (tmp_path / "ready").stat().st_ino == before.st_ino
        assert runtime.validate_ready().deployment_id == deployment.deployment_id
        assert not (tmp_path / ".ready-pending").exists()
    async with open_local_durability(config) as reopened:
        assert reopened.validate_ready().deployment_id == deployment.deployment_id


@pytest.mark.asyncio
async def test_ready_marker_cannot_claim_a_different_deployment(tmp_path):
    from uuid import uuid4

    from test_local_durable_store import IDENTITY, canonical, manifest

    config = LocalDurability(directory=tmp_path, revision="r1")
    async with open_local_durability(config) as runtime:
        with runtime.store.acquire_deployment():
            deployment = runtime.store.initialize_enrollment(
                canonical(IDENTITY), "a" * 64
            )
            runtime.store.bind_deployment(
                deployment.deployment_id, manifest(deployment)
            )
            marker = tmp_path / ".ready-pending"
            marker.write_text(
                canonical({"format_version": 1, "deployment_id": str(uuid4())})
            )
            marker.chmod(0o600)
            before = marker.read_bytes()
            with pytest.raises(ConfigurationError) as caught:
                runtime.publish_ready()
            assert caught.value.reason == "durable_deployment_mismatch"
            assert marker.read_bytes() == before
            assert not (tmp_path / "ready").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["checkpoints", "writes"])
@pytest.mark.parametrize("journal", [False, True])
async def test_populated_native_schema_without_bound_journal_is_never_adopted(
    tmp_path, table, journal
):
    import shutil

    config = LocalDurability(directory=tmp_path, revision="r1")
    async with open_local_durability(config) as runtime:
        conn = runtime.resources.checkpointer.conn
        if table == "checkpoints":
            await conn.execute(
                "INSERT INTO checkpoints(thread_id,checkpoint_id) VALUES ('foreign','foreign-checkpoint')"
            )
        else:
            await conn.execute(
                "INSERT INTO writes(thread_id,checkpoint_id,task_id,idx,channel) VALUES ('foreign','foreign-checkpoint','foreign-task',0,'files')"
            )
        await conn.commit()
    if not journal:
        (tmp_path / "execution.sqlite").unlink()
        shutil.rmtree(tmp_path / ".locks")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    with pytest.raises(ConfigurationError) as caught:
        async with open_local_durability(config):
            pytest.fail("populated saver requires an immutable bound journal")
    assert caught.value.reason == "durable_deployment_mismatch"
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before
    if not journal:
        assert not (tmp_path / ".locks").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary", ["ready_created", "ready_written", "ready_synced", "ready_linked"]
)
async def test_ready_publication_recovers_after_process_termination(
    tmp_path, boundary, monkeypatch
):
    import subprocess
    import sys
    from pathlib import Path

    from test_local_durable_store import IDENTITY, canonical, manifest

    import openeight
    from openeight.deepagents._local_durable import _runtime

    config = LocalDurability(directory=tmp_path, revision="r1")
    async with open_local_durability(config) as runtime:
        with runtime.store.acquire_deployment():
            deployment = runtime.store.initialize_enrollment(
                canonical(IDENTITY), "a" * 64
            )
            runtime.store.bind_deployment(
                deployment.deployment_id, manifest(deployment)
            )
    script = """
import asyncio, os, sys
from pathlib import Path
from openeight.deepagents._local_durable import _runtime
from openeight.deepagents._local_durable._runtime import open_local_durability
assert Path(_runtime.__file__).resolve() == Path(sys.argv[3]).resolve(), "SDK origin mismatch"
from openeight.deepagents.durable import LocalDurability
async def run():
    async with open_local_durability(LocalDurability(directory=sys.argv[1], revision="r1")) as runtime:
        runtime.store.fault = lambda point: os._exit(83) if point == sys.argv[2] else None
        with runtime.store.acquire_deployment():
            runtime.publish_ready()
asyncio.run(run())
"""
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(openeight.__file__).resolve().parent.parent),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path),
            boundary,
            str(Path(_runtime.__file__).resolve()),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 83, result.stderr
    async with open_local_durability(config) as reopened:
        import stat

        synced_directories = []
        original_fsync = os.fsync

        def fsync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                synced_directories.append(True)
            return original_fsync(descriptor)

        monkeypatch.setattr(os, "fsync", fsync)
        with reopened.store.acquire_deployment():
            reopened.publish_ready()
        assert synced_directories
        assert not (tmp_path / ".ready-pending").exists()
        assert reopened.validate_ready().deployment_id == deployment.deployment_id


@pytest.mark.asyncio
async def test_explicit_empty_initialization_recovers_partial_matching_marker(tmp_path):
    from test_local_durable_store import IDENTITY, canonical, manifest

    config = LocalDurability(directory=tmp_path, revision="r1")
    async with open_local_durability(config) as runtime:
        with runtime.store.acquire_deployment():
            deployment = runtime.store.initialize_enrollment(
                canonical(IDENTITY), "a" * 64
            )
            runtime.store.bind_deployment(
                deployment.deployment_id, manifest(deployment)
            )
            expected = canonical(
                {"format_version": 1, "deployment_id": deployment.deployment_id}
            )
            pending = tmp_path / ".ready-pending"
            pending.write_text(expected[: len(expected) // 2])
            pending.chmod(0o600)
            runtime.publish_ready()
        assert (tmp_path / "ready").read_text() == expected


@pytest.mark.asyncio
async def test_foreign_checkpoint_rows_committed_only_in_wal_reject_without_source_changes(
    tmp_path,
):
    import hashlib
    import subprocess
    import sys
    from pathlib import Path

    script = """
import asyncio, os, sys
import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
async def run():
    path = sys.argv[1] + "/checkpoints.sqlite"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    connection = await aiosqlite.connect(path)
    saver = AsyncSqliteSaver(connection)
    await saver.setup()
    await connection.execute("INSERT INTO checkpoints(thread_id,checkpoint_id) VALUES ('foreign','foreign')")
    await connection.commit()
    os._exit(84)
asyncio.run(run())
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 84, result.stderr
    assert (tmp_path / "checkpoints.sqlite-wal").stat().st_size > 0

    def signatures():
        return {
            p.name: (p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in tmp_path.iterdir()
        }

    before = signatures()
    with pytest.raises(ConfigurationError) as caught:
        async with open_local_durability(
            LocalDurability(directory=tmp_path, revision="r1")
        ):
            pytest.fail("foreign WAL rows must not be adopted")
    assert caught.value.reason == "durable_deployment_mismatch"
    assert signatures() == before
