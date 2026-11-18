"""Fence native saver operations under application-authorized thread ownership."""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from importlib.metadata import version

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from openeight.checkpoints import CheckpointBinding
from openeight.errors import ConfigurationError

CURRENT_CHECKPOINT_CLAIM = ContextVar("openeight_checkpoint_claim", default=None)


def invalid():
    raise ConfigurationError("checkpoint_scope_invalid")


@dataclass(eq=False)
class ClaimScope:
    claim: object = field(repr=False)
    binding: CheckpointBinding
    configuration_sha256: str = field(repr=False)
    model_variant: str | None
    execution_id: str | None = None
    read_only: bool = True
    active: bool = True
    calls: set = field(default_factory=set, repr=False)

    async def assert_held(self):
        if not self.active or self.claim.binding != self.binding:
            invalid()
        failed = False
        try:
            await self.claim.assert_held()
        except Exception:
            failed = True
        if failed:
            invalid()

    def selectors(self, config):
        if type(config) is not dict or type(config.get("configurable")) is not dict:
            invalid()
        values = config["configurable"]
        namespace = values.get("checkpoint_ns", "")
        root = self.binding.checkpoint_ns
        if (
            values.get("thread_id") != self.binding.thread_id
            or type(namespace) is not str
            or namespace != root
            and (root and not namespace.startswith(root + "|"))
            or len(namespace.encode()) > 16384
            or "\0" in namespace
            or values.get("checkpoint_id") is not None
            and (
                type(values["checkpoint_id"]) is not str or not values["checkpoint_id"]
            )
        ):
            invalid()
        mapping = values.get("checkpoint_map", {})
        if type(mapping) is not dict or any(
            type(key) is not str
            or type(value) is not str
            or not value
            or root
            and key != root
            and not key.startswith(root + "|")
            for key, value in mapping.items()
        ):
            invalid()
        return values

    async def call(self, operation):
        await self.assert_held()
        task = asyncio.create_task(operation())
        self.calls.add(task)
        cancelled = False
        failed = False
        result = None
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break
            try:
                result = task.result()
            except Exception:
                failed = True
        finally:
            self.calls.discard(task)
        if cancelled:
            raise asyncio.CancelledError
        if failed:
            raise ConfigurationError("checkpoint_persistence_failed")
        await self.assert_held()
        return result

    async def drain(self):
        while self.calls:
            await asyncio.gather(
                *(asyncio.shield(task) for task in tuple(self.calls)),
                return_exceptions=True,
            )


async def assert_checkpoint_claim():
    scope = CURRENT_CHECKPOINT_CLAIM.get()
    if scope is not None:
        await scope.assert_held()


class ScopedCheckpointAccess(BaseCheckpointSaver):
    def __init__(self, base):
        kind = (type(base).__module__, type(base).__name__)
        allowed = {
            ("langgraph.checkpoint.sqlite.aio", "AsyncSqliteSaver"): "async-sqlite"
        }
        postgres = ("langgraph.checkpoint.postgres.aio", "AsyncPostgresSaver")
        if kind == postgres:
            expected = {
                "langgraph-checkpoint-postgres": "3.1.0",
                "psycopg": "3.3.8",
                "psycopg-pool": "3.3.3",
            }
            if any(version(name) != pin for name, pin in expected.items()):
                raise ConfigurationError("unsupported_checkpointer")
            allowed[postgres] = "async-postgres"
        if (
            kind not in allowed
            or type(base.serde) is not JsonPlusSerializer
            or base.serde.pickle_fallback is not False
        ):
            raise ConfigurationError("unsupported_checkpointer")
        super().__init__(serde=base.serde)
        self.base, self.kind = base, allowed[kind]

    def scope(self, config, *, write=False):
        scope = CURRENT_CHECKPOINT_CLAIM.get()
        if scope is None or not scope.active or write and scope.read_only:
            invalid()
        scope.selectors(config)
        return scope

    async def aget_tuple(self, config):
        scope = self.scope(config)
        result = await scope.call(lambda: self.base.aget_tuple(config))
        if result is not None:
            if (
                type(result.checkpoint) is not dict
                or type(result.checkpoint.get("v")) is not int
                or result.checkpoint["v"] != 4
            ):
                invalid()
            selected = scope.selectors(result.config)
            requested = config["configurable"]
            if (
                selected.get("checkpoint_ns", "") != requested.get("checkpoint_ns", "")
                or requested.get("checkpoint_id") is not None
                and selected.get("checkpoint_id") != requested["checkpoint_id"]
            ):
                invalid()
            if result.parent_config is not None:
                parent = scope.selectors(result.parent_config)
                if parent.get("checkpoint_ns", "") != selected.get("checkpoint_ns", ""):
                    invalid()
        return result

    async def aput(self, config, checkpoint, metadata, new_versions):
        scope = self.scope(config, write=True)
        metadata = {
            **metadata,
            "openeight_checkpoint": {
                "configuration_sha256": scope.configuration_sha256,
                "graph_revision": scope.binding.graph_revision,
                "model_variant": scope.model_variant,
            },
        }
        result = await scope.call(
            lambda: self.base.aput(config, checkpoint, metadata, new_versions)
        )
        scope.selectors(result)
        return result

    async def aput_writes(self, config, *args, **kwargs):
        scope = self.scope(config, write=True)
        return await scope.call(lambda: self.base.aput_writes(config, *args, **kwargs))

    async def aget_delta_channel_history(self, *, config, **kwargs):
        scope = self.scope(config)
        return await scope.call(
            lambda: self.base.aget_delta_channel_history(config=config, **kwargs)
        )

    def get_next_version(self, current, channel):
        return self.base.get_next_version(current, channel)
