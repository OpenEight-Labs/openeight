"""Borrow a saver without letting caller thread selectors escape this owner."""

from uuid import uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver

from openeight.errors import ConfigurationError


class ScopedSaver(BaseCheckpointSaver):
    def __init__(self, base):
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        if not isinstance(base, AsyncSqliteSaver) or getattr(
            base.serde, "pickle_fallback", False
        ):
            raise ConfigurationError("unsupported_checkpointer")
        super().__init__(serde=base.serde)
        self.base, self.prefix, self.threads = base, str(uuid4()) + "/", {}

    def new_thread(self):
        if len(self.threads) >= 1000:
            raise ConfigurationError("execution_busy")
        thread = str(uuid4())
        self.threads[thread] = {"busy": False, "pending": False}
        return thread

    def release_thread(self, thread_id):
        state = self.threads.get(thread_id)
        if state is None:
            raise ConfigurationError("checkpoint_thread_unowned")
        if state["busy"]:
            raise ConfigurationError("checkpoint_thread_busy")
        del self.threads[thread_id]

    def encode(self, config):
        selectors = dict(config.get("configurable", {}))
        thread = selectors.get("thread_id")
        if thread not in self.threads:
            raise ConfigurationError("checkpoint_thread_unowned")
        selectors["thread_id"] = self.prefix + thread
        return {**config, "configurable": selectors}

    def decode(self, config):
        if config is None:
            return None
        selectors = dict(config.get("configurable", {}))
        physical = selectors.get("thread_id")
        if not isinstance(physical, str) or not physical.startswith(self.prefix):
            raise ConfigurationError("checkpoint_thread_unowned")
        thread = physical[len(self.prefix) :]
        if thread not in self.threads:
            raise ConfigurationError("checkpoint_thread_unowned")
        selectors["thread_id"] = thread
        return {**config, "configurable": selectors}

    async def aget_tuple(self, config):
        result = await self.base.aget_tuple(self.encode(config))
        if result is None:
            return None
        return result._replace(
            config=self.decode(result.config),
            parent_config=self.decode(result.parent_config),
        )

    async def aput(self, config, *args, **kwargs):
        return self.decode(await self.base.aput(self.encode(config), *args, **kwargs))

    async def aput_writes(self, config, *args, **kwargs):
        return await self.base.aput_writes(self.encode(config), *args, **kwargs)

    async def aget_delta_channel_history(self, *, config, **kwargs):
        return await self.base.aget_delta_channel_history(
            config=self.encode(config), **kwargs
        )

    def get_next_version(self, current, channel):
        return self.base.get_next_version(current, channel)
