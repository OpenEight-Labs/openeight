"""Compose managed owners inside the application's Starlette lifespan."""

from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, AsyncContextManager, Callable

from openeight.errors import ConfigurationError
from openeight.langchain._managed import ManagedAgent


def lifespan(
    *agents: ManagedAgent,
    existing: Callable[[Any], AsyncContextManager[Any]] | None = None,
) -> Callable[[Any], AsyncContextManager[Any]]:
    """Enter existing lifespan first, then agents; unwind resources in reverse order."""

    if (
        len({id(agent) for agent in agents}) != len(agents)
        or any(not isinstance(agent, ManagedAgent) for agent in agents)
        or existing is not None
        and not callable(existing)
    ):
        raise ConfigurationError("integration_resource_conflict")

    @asynccontextmanager
    async def managed_lifespan(app):
        async with AsyncExitStack() as stack:
            state = await stack.enter_async_context(existing(app)) if existing else None
            for agent in agents:
                await stack.enter_async_context(agent)
            yield state

    return managed_lifespan
