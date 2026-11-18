"""Binding validation tolerates SQLite cleanup without weakening state checks."""

from pathlib import Path
import pytest
from configuration import load_binding
from initialization import setup
from local import LocalError


@pytest.fixture
def binding(tmp_path, monkeypatch):
    for name in ("OPENEIGHT_ENDPOINT", "OPENEIGHT_API_KEY", "OPENEIGHT_AGENT_NAME"):
        monkeypatch.delenv(name, raising=False)
    state = tmp_path / "state"
    setup(state, deployment_id="sidecar-test", mode="standalone", apply=True)
    return state, load_binding(state)


def enumerate_extra(monkeypatch, state, extra, prepare=lambda: None):
    original = Path.iterdir

    def entries(path):
        result = list(original(path))
        if path == state:
            prepare()
            result.append(extra)
        return iter(result)

    monkeypatch.setattr(Path, "iterdir", entries)


@pytest.mark.parametrize("name", [
    "retailer.sqlite-wal", "retailer.sqlite-shm",
    "recovery.sqlite-wal", "recovery.sqlite-shm",
])
def test_binding_accepts_sidecar_removed_after_enumeration(binding, monkeypatch, name):
    state, expected = binding
    enumerate_extra(monkeypatch, state, state / name)
    assert load_binding(state) == expected


def test_binding_does_not_ignore_unknown_missing_sidecar(binding, monkeypatch):
    state, _ = binding
    enumerate_extra(monkeypatch, state, state / "unknown.sqlite-wal")
    with pytest.raises(LocalError, match="setup_not_ready"):
        load_binding(state)


@pytest.mark.parametrize("name", ["retailer.sqlite", "recovery.sqlite"])
def test_binding_does_not_ignore_missing_primary_database(binding, monkeypatch, name):
    state, _ = binding
    original = Path.lstat
    scanning = False
    original_entries = Path.iterdir

    def entries(path):
        nonlocal scanning
        result = list(original_entries(path))
        if path == state:
            scanning = True
        return iter(result)

    def metadata(path, *args, **kwargs):
        if scanning and path == state / name:
            raise FileNotFoundError(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "iterdir", entries)
    monkeypatch.setattr(Path, "lstat", metadata)
    # The original implementation uses stat; validate the same missing-primary
    # interleaving before and after the lstat change.
    original_stat = Path.stat

    def missing_stat(path, *args, **kwargs):
        if scanning and path == state / name:
            raise FileNotFoundError(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", missing_stat)
    with pytest.raises(LocalError, match="setup_not_ready"):
        load_binding(state)


@pytest.mark.parametrize("unsafe", ["permissions", "symlink"])
def test_binding_rejects_unsafe_optional_sidecar(binding, tmp_path, monkeypatch, unsafe):
    state, _ = binding
    sidecar = state / "recovery.sqlite-wal"
    target = tmp_path / "target"
    target.write_bytes(b"private")
    target.chmod(0o600)

    def prepare():
        if unsafe == "symlink":
            sidecar.symlink_to(target)
        else:
            sidecar.write_bytes(b"private")
            sidecar.chmod(0o644)

    enumerate_extra(monkeypatch, state, sidecar, prepare)
    with pytest.raises(LocalError, match="insecure_state"):
        load_binding(state)


def test_binding_does_not_ignore_sidecar_permission_error(binding, monkeypatch):
    state, _ = binding
    sidecar = state / "recovery.sqlite-shm"
    enumerate_extra(monkeypatch, state, sidecar)
    original_lstat, original_stat = Path.lstat, Path.stat

    def metadata(path, *args, **kwargs):
        if path == sidecar:
            raise PermissionError(path)
        return original_lstat(path, *args, **kwargs)

    def stat_metadata(path, *args, **kwargs):
        if path == sidecar:
            raise PermissionError(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", metadata)
    monkeypatch.setattr(Path, "stat", stat_metadata)
    with pytest.raises(LocalError, match="setup_not_ready"):
        load_binding(state)
