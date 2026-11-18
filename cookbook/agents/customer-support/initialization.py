"""Local initialization; humans own Agent credentials and governance in OpenEight Console."""

from dataclasses import asdict
import os
from pathlib import Path
import re

from local import (
    Deployment,
    LocalError,
    Locks,
    canonical,
    now,
    protected_directory,
    protected_secret,
)
from fixtures import seed_retailer
from storage import SQLiteExecutionStore


def write_private(path, text, *, exclusive=False):
    path = Path(path)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_NOFOLLOW
        | (os.O_EXCL if exclusive else os.O_TRUNC)
    )
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def intended_changes(deployment, mode="standalone", **labels):
    return {
        "deployment": deployment,
        "mode": mode,
        "storage": "local synthetic SQLite state",
        "governance": "human configuration in OpenEight Console"
        if mode == "integrated"
        else "no OpenEight integration",
    }


def setup(
    state,
    *,
    deployment_id,
    mode="standalone",
    endpoint=None,
    agent_name=None,
    api_key_file=None,
    api_key=None,
    workspace_id=None,
    agent_id=None,
    api_key_id=None,
    apply=False,
):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", deployment_id):
        raise LocalError("invalid_deployment")
    if not apply:
        return intended_changes(deployment_id, mode)
    state = Path(state)
    with Locks(state).hold("lifecycle"):
        protected_directory(state)
        if any(state.iterdir()):
            raise LocalError("setup_state_exists")
        d = Deployment(
            deployment_id,
            endpoint,
            workspace_id,
            agent_name,
            agent_id,
            api_key_id,
            mode,
            4,
            now(),
        )
        d.validate()
        key = None
        if mode == "integrated":
            from openeight import Client

            if api_key is not None and api_key_file is not None:
                raise LocalError("ambiguous_agent_key")
            if api_key is None and api_key_file is None:
                raise LocalError("agent_key_required")
            key = api_key if api_key is not None else protected_secret(api_key_file)
            # Client construction validates public configuration without making requests.
            with Client(endpoint, api_key=key, agent_name=agent_name):
                pass
        elif api_key_file is not None or api_key is not None:
            raise LocalError("standalone_credentials_forbidden")
        if key:
            write_private(state / "machine-key", key, exclusive=True)
        seed_retailer(state, d)
        SQLiteExecutionStore(state, d).initialize()
        write_private(state / "deployment.json", canonical(asdict(d)), exclusive=True)
        write_private(
            state / "ready.tmp", canonical({"deployment_id": d.id}), exclusive=True
        )
        os.replace(state / "ready.tmp", state / "ready")
        return {"status": "local_ready", "mode": d.mode, "deployment_id": d.id}
