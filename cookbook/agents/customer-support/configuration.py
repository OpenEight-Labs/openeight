"""Deployment identity and explicit local configuration validation."""

import hmac
import json
import os
import stat
from urllib.parse import urlsplit
from local import (
    Deployment, LocalError, connection, protected_directory,
    protected_secret, read_deployment,
)


def load_binding(state, *, runtime=False):
    state = protected_directory(state)
    try:
        d = Deployment(**json.loads(protected_secret(state / "deployment.json")))
        d.validate()
        if json.loads(protected_secret(state / "ready")) != {"deployment_id": d.id}:
            raise LocalError("setup_not_ready")
        for filename in ("retailer.sqlite", "recovery.sqlite"):
            with connection(state / filename) as db:
                if read_deployment(db) != d:
                    raise LocalError("deployment_mismatch")
        sidecars = {
            name + suffix
            for name in ("retailer.sqlite", "recovery.sqlite")
            for suffix in ("-wal", "-shm")
        }
        for p in state.iterdir():
            if p.name.endswith((".sqlite", "-wal", "-shm")):
                try:
                    metadata = p.lstat()
                except FileNotFoundError:
                    # SQLite removes optional sidecars when its last connection closes.
                    if p.name in sidecars:
                        continue
                    raise
                if stat.S_ISLNK(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                    raise LocalError("insecure_state")
        if runtime and d.mode == "standalone":
            if (state / "machine-key").exists():
                raise LocalError("execution_mode_mismatch")
            return d, None
        if runtime:
            for name, expected in (
                ("OPENEIGHT_ENDPOINT", d.endpoint),
                ("OPENEIGHT_AGENT_NAME", d.agent_name),
            ):
                if os.environ.get(name, expected) != expected:
                    raise LocalError("deployment_mismatch")
            key = protected_secret(state / "machine-key")
            if "OPENEIGHT_API_KEY" in os.environ and not hmac.compare_digest(
                os.environ["OPENEIGHT_API_KEY"], key
            ):
                raise LocalError("credential_rotation_refused")
            return d, key
        return d
    except (ValueError, TypeError, OSError):
        raise LocalError("setup_not_ready") from None


def validate_console_origin(value):
    if value is None:
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError
        parsed.port
    except ValueError:
        raise LocalError("invalid_console_origin") from None
    return value.rstrip("/")


def resolve_console_origin(deployment, override=None):
    if deployment.mode != "integrated":
        return None
    selected = override if override is not None else os.environ.get("OPENEIGHT_CONSOLE_ORIGIN")
    endpoint = urlsplit(deployment.endpoint)
    return validate_console_origin(selected or f"{endpoint.scheme}://{endpoint.netloc}")
