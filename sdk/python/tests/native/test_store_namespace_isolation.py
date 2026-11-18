"""Qualified native stores preserve namespace isolation across public operations."""

import os
from contextlib import contextmanager
from uuid import uuid4

import pytest
from langgraph.store.base import GetOp, InvalidNamespaceError, PutOp


@contextmanager
def postgres_schema():
    dsn = os.environ.get("OPENEIGHT_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Requires the isolated PostgreSQL qualification database")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = "openeight_store_qualification_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            yield make_conninfo(dsn, options=f"-csearch_path={schema}")
        finally:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


@pytest.fixture(params=["sqlite", "postgres"])
def store(request):
    if request.param == "sqlite":
        module = pytest.importorskip("langgraph.store.sqlite")
        with module.SqliteStore.from_conn_string(":memory:") as value:
            value.setup()
            yield value
    else:
        module = pytest.importorskip("langgraph.store.postgres")
        with postgres_schema() as dsn:
            with module.PostgresStore.from_conn_string(dsn) as value:
                value.setup()
                yield value


def test_store_rejects_namespace_aliases_without_changing_other_items(store):
    namespace = ("tenant", "bob")
    store.put(namespace, "item", {"owner": "bob"})
    for invalid in [("tenant.bob",), ("tenant", ""), ("tenant", 7)]:
        for operation in [
            lambda: store.get(invalid, "item"),
            lambda: store.search(invalid),
            lambda: store.delete(invalid, "item"),
            lambda: store.list_namespaces(prefix=invalid),
            lambda: store.batch([GetOp(invalid, "item")]),
            lambda: store.batch([PutOp(invalid, "item", {"owner": "other"})]),
        ]:
            with pytest.raises(InvalidNamespaceError):
                operation()
    assert store.get(namespace, "item").value == {"owner": "bob"}
    store.delete(namespace, "item")
    assert store.get(namespace, "item") is None


def test_store_search_and_namespace_filters_respect_segment_boundaries(store):
    for label in ["alice", "alice2", "user_1", "userX1", "user%1", "userZZ1"]:
        store.put((label, "memory"), "item", {"owner": label})
    store.put(("alice", "memory", "child"), "item", {"owner": "alice"})
    for label in ["alice", "user_1", "user%1"]:
        assert {item.value["owner"] for item in store.search((label,))} == {label}
        expected = {(label, "memory")}
        if label == "alice":
            expected.add((label, "memory", "child"))
        assert set(store.list_namespaces(prefix=(label,))) == expected
    store.put(("users", "malice"), "item", {"owner": "other"})
    store.put(("users", "alice"), "item", {"owner": "alice"})
    assert store.list_namespaces(suffix=("alice",)) == [("users", "alice")]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_async_store_rejects_aliases_and_preserves_scope(backend):
    async def check(store):
        await store.setup()
        await store.aput(("tenant", "bob"), "item", {"owner": "bob"})
        await store.aput(("tenant", "bob2"), "item", {"owner": "bob2"})
        alias = ("tenant.bob",)
        for operation in [
            lambda: store.aget(alias, "item"),
            lambda: store.asearch(alias),
            lambda: store.adelete(alias, "item"),
            lambda: store.alist_namespaces(prefix=alias),
            lambda: store.abatch([GetOp(alias, "item")]),
            lambda: store.abatch([PutOp(alias, "item", {"owner": "other"})]),
        ]:
            with pytest.raises(InvalidNamespaceError):
                await operation()
        assert (await store.aget(("tenant", "bob"), "item")).value == {"owner": "bob"}
        assert [item.value for item in await store.asearch(("tenant", "bob"))] == [
            {"owner": "bob"}
        ]
        await store.adelete(("tenant", "bob"), "item")
        assert await store.aget(("tenant", "bob"), "item") is None

    if backend == "sqlite":
        module = pytest.importorskip("langgraph.store.sqlite.aio")
        async with module.AsyncSqliteStore.from_conn_string(":memory:") as store:
            await check(store)
    else:
        module = pytest.importorskip("langgraph.store.postgres.aio")
        with postgres_schema() as dsn:
            async with module.AsyncPostgresStore.from_conn_string(dsn) as store:
                await check(store)
