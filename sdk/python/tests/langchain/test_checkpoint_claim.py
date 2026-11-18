"""Real saver commits remain inside application-owned checkpoint claims."""

import asyncio

import pytest

pytest.importorskip("langgraph.checkpoint.sqlite.aio")

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.pregel._checkpoint import empty_checkpoint

from openeight import ConfigurationError
from openeight.checkpoints import CheckpointBinding
from openeight.langchain._checkpoint_claim import (
    CURRENT_CHECKPOINT_CLAIM,
    ClaimScope,
    ScopedCheckpointAccess,
)


class ApplicationClaim:
    def __init__(self):
        self.binding = CheckpointBinding(
            scope_id="authenticated-scope",
            thread_id="application-thread",
            checkpoint_ns="",
            graph_revision="release-1",
            model_variant=None,
            mode="new",
        )
        self.authorized = True

    async def assert_held(self):
        if not self.authorized:
            raise RuntimeError("private application authority details")


@pytest.mark.asyncio
async def test_real_sqlite_claim_fences_selectors_and_records_sdk_metadata(tmp_path):
    async with AsyncSqliteSaver.from_conn_string(
        str(tmp_path / "state.sqlite")
    ) as native:
        saver = ScopedCheckpointAccess(native)
        claim = ApplicationClaim()
        scope = ClaimScope(
            claim,
            claim.binding,
            "a" * 64,
            None,
            execution_id="execution",
            read_only=False,
        )
        token = CURRENT_CHECKPOINT_CLAIM.set(scope)
        try:
            config = {
                "configurable": {"thread_id": "application-thread", "checkpoint_ns": ""}
            }
            with pytest.raises(ConfigurationError):
                await saver.aput(
                    {"configurable": {"thread_id": "foreign"}},
                    empty_checkpoint(),
                    {},
                    {},
                )
            assert await native.aget_tuple(config) is None
            saved = await saver.aput(config, empty_checkpoint(), {}, {})
            actual = await saver.aget_tuple(saved)
            assert actual.metadata["openeight_checkpoint"] == {
                "configuration_sha256": "a" * 64,
                "graph_revision": "release-1",
                "model_variant": None,
            }
            claim.authorized = False
            with pytest.raises(ConfigurationError) as caught:
                await saver.aput(config, empty_checkpoint(), {}, {})
            assert caught.value.reason == "checkpoint_scope_invalid"
            assert caught.value.__context__ is None
            assert (await native.aget_tuple(config)).config == saved
        finally:
            CURRENT_CHECKPOINT_CLAIM.reset(token)
        with pytest.raises(ConfigurationError):
            await saver.aget_tuple(config)


@pytest.mark.asyncio
async def test_cancelling_real_sqlite_commit_retains_claim_until_commit_settles(
    tmp_path,
):
    async with AsyncSqliteSaver.from_conn_string(
        str(tmp_path / "state.sqlite")
    ) as native:
        await native.setup()
        saver = ScopedCheckpointAccess(native)
        claim = ApplicationClaim()
        scope = ClaimScope(claim, claim.binding, "a" * 64, None, read_only=False)
        token = CURRENT_CHECKPOINT_CLAIM.set(scope)
        config = {
            "configurable": {"thread_id": "application-thread", "checkpoint_ns": ""}
        }
        try:
            await native.lock.acquire()
            checkpoint = empty_checkpoint()
            task = asyncio.create_task(saver.aput(config, checkpoint, {}, {}))
            while not scope.calls:
                await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and scope.calls
            native.lock.release()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not scope.calls
            assert (await native.aget_tuple(config)).checkpoint["id"] == checkpoint[
                "id"
            ]
        finally:
            if native.lock.locked():
                native.lock.release()
            CURRENT_CHECKPOINT_CLAIM.reset(token)
