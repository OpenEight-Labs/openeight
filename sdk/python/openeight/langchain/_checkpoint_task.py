"""Keep claim acquisition, execution and release in one cancellation-owned context."""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class _Control:
    settling: bool = False


_CURRENT_CONTROL = ContextVar("openeight_claim_task_control", default=None)


def begin_settlement():
    control = _CURRENT_CONTROL.get()
    if control is not None:
        control.settling = True


async def _owned(operation, control):
    token = _CURRENT_CONTROL.set(control)
    try:
        return await operation()
    finally:
        _CURRENT_CONTROL.reset(token)


async def invoke(operation):
    control = _Control()
    task = asyncio.create_task(_owned(operation, control))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not cancelled:
                cancelled = True
                if not control.settling:
                    task.cancel()
        except BaseException:
            break
    if cancelled:
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


async def stream(factory):
    queue = asyncio.Queue(maxsize=1)

    async def produce():
        iterator = factory()
        try:
            async for item in iterator:
                acknowledged = asyncio.get_running_loop().create_future()
                await queue.put((item, acknowledged))
                await acknowledged
        finally:
            await iterator.aclose()

    control = _Control()
    task = asyncio.create_task(_owned(produce, control))
    primary = None
    try:
        while True:
            waiting = asyncio.create_task(queue.get())
            try:
                done, _ = await asyncio.wait(
                    (task, waiting), return_when=asyncio.FIRST_COMPLETED
                )
                if waiting not in done:
                    task.result()
                    return
                value, acknowledged = waiting.result()
            finally:
                if not waiting.done():
                    waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
            yield value
            acknowledged.set_result(None)
    except BaseException as error:
        primary = error
        raise
    finally:
        if not task.done() and not control.settling:
            task.cancel()
        from ._session import settle

        try:
            await settle(task)
        except asyncio.CancelledError:
            if primary is None and not task.cancelled():
                raise
        except BaseException:
            if primary is None:
                raise
