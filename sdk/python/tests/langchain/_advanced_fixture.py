"""Application-owned verified enrollment over shared journal/continuation primitives."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from openeight._local_storage import (
    LocalError,
    check_sidecars,
    now,
    protected_file,
    transaction,
)
from openeight.deepagents import _deployment
from openeight.deepagents._local_durable._schema import SCHEMA_SQL
from openeight.deepagents._local_durable._store import SQLiteDeepAgentsStore
from openeight.managed import ManagedResources

ADVANCED_SCHEMA = SCHEMA_SQL.replace(
    "CHECK(identity_kind='credential_bound')",
    "CHECK(identity_kind='verified_issuance')",
)
for field in ("organization_id", "workspace_id", "agent_id", "api_key_id"):
    ADVANCED_SCHEMA = ADVANCED_SCHEMA.replace(
        field + " TEXT DEFAULT NULL CHECK(" + field + " IS NULL)",
        field + " TEXT NOT NULL",
    )


class AdvancedSQLiteStore(SQLiteDeepAgentsStore):
    _schema_sql = ADVANCED_SCHEMA

    def initialize_enrollment(self, identity_json, credential_binding_sha256):
        self._owned()
        value = _deployment.identity(identity_json)
        if value["kind"] != "verified_issuance":
            raise LocalError("deployment_mismatch")
        _deployment.digest(credential_binding_sha256)
        with self._db() as db, transaction(db):
            if db.execute("SELECT 1 FROM deployments").fetchone():
                existing = self._read_deployment(db)
                if (
                    existing.identity_json != identity_json
                    or existing.credential_binding_sha256 != credential_binding_sha256
                ):
                    raise LocalError("deployment_mismatch")
                return existing
            self._empty(db)
            identifier = str(uuid4())
            db.execute(
                "INSERT INTO deployments(id,format_version,created_at,identity_kind,endpoint,agent_name,organization_id,workspace_id,agent_id,api_key_id,credential_binding_sha256) VALUES (?,1,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    now(),
                    value["kind"],
                    value["endpoint"],
                    value["agent_name"],
                    value["organization_id"],
                    value["workspace_id"],
                    value["agent_id"],
                    value["api_key_id"],
                    credential_binding_sha256,
                ),
            )
            self._deployment_id = identifier
            return self._read_deployment(db)


@asynccontextmanager
async def open_advanced_durability(options):
    store = AdvancedSQLiteStore(options.directory)
    path = protected_file(store.state / "checkpoints.sqlite", create=True)
    check_sidecars(path)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        saver.serde = JsonPlusSerializer(pickle_fallback=False)
        await saver.setup()
        yield SimpleNamespace(
            store=store, resources=ManagedResources(checkpointer=saver)
        )
