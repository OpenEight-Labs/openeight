"""LangChain resolution remains covered without optional provider packages."""

import asyncio

import pytest

from openeight import ConfigurationError
from openeight.langchain import create_agent

CONFIG = {
    "agent_name": "support",
    "endpoint": "https://control.example",
    "credential": "ctk_test",
}


def test_invalid_model_resolution_is_sanitized_without_cause():
    factory = create_agent
    managed = factory("unknown-provider:SECRET-SENTINEL", openeight=CONFIG)

    async def run():
        session = managed._factory()
        with pytest.raises(ConfigurationError) as caught:
            await session._prepare(
                resources=session._resources, governance=session._governance
            )
        assert caught.value.__context__ is None
        assert caught.value.__cause__ is None
        assert "SECRET-SENTINEL" not in str(caught.value)

    asyncio.run(run())
