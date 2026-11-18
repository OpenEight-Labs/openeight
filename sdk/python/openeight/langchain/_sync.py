"""One private loop per synchronous owner; all execution uses async enforcement."""

from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures import Future, InvalidStateError
from contextvars import copy_context
from typing import Any, Self

from openeight.errors import ConfigurationError


class SyncBridge:
    def __init__(self, owner):
        self.owner = owner
        self.pid = os.getpid()
        self.loop = None
        self.thread = None
        self.busy = threading.Lock()
        self.streams = set()
        self.stream_lock = threading.Lock()
        self.stopped = False

    def _check(self):
        from ._runtime import CURRENT_INVOCATION

        invocation = CURRENT_INVOCATION.get()
        if invocation is not None and invocation.owner is self.owner._owner:
            raise ConfigurationError("integration_sync_reentry")
        if os.getpid() != self.pid:
            raise ConfigurationError("instrumentation_process_changed")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise ConfigurationError("integration_sync_reentry")
        if self.stopped:
            if self.owner._cleanup_error:
                raise ConfigurationError("integration_resource_cleanup_failed")
            raise ConfigurationError("integration_closed")

    def enter(self):
        self._check()
        ready = threading.Event()

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.loop = loop
            ready.set()
            try:
                loop.run_forever()
            finally:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(loop.shutdown_default_executor())
                loop.close()

        self.thread = threading.Thread(target=run, name="openeight-loop", daemon=True)
        self.thread.start()
        ready.wait()
        try:
            return self.call(self.owner._enter, category="lifecycle")
        except BaseException:
            self._stop()
            raise

    def _submit(self, operation, *, release=False):
        context = copy_context()
        result = Future()
        state = {"task": None}

        async def execute():
            return await operation()

        def finish(task):
            if release:
                self.busy.release()
            if task.cancelled():
                result.cancel()
                return
            error = task.exception()
            try:
                if error is None:
                    result.set_result(task.result())
                else:
                    result.set_exception(error)
            except InvalidStateError:
                # Caller cancellation can race task settlement after ownership release.
                pass

        def start():
            if result.cancelled():
                if release:
                    self.busy.release()
                return
            task = self.loop.create_task(execute(), context=context)
            state["task"] = task
            task.add_done_callback(finish)

        def cancelled(future):
            if future.cancelled():

                def cancel_task():
                    if state["task"] is not None:
                        state["task"].cancel()

                self.loop.call_soon_threadsafe(cancel_task)

        result.add_done_callback(cancelled)
        self.loop.call_soon_threadsafe(start, context=context)
        return result

    def call(self, operation, *, category):
        self._check()
        execution = category == "execution"
        if execution and not self.busy.acquire(blocking=False):
            raise ConfigurationError("integration_busy")
        try:
            future = self._submit(operation, release=execution)
        except BaseException:
            if execution:
                self.busy.release()
            raise
        try:
            return future.result()
        except KeyboardInterrupt:
            future.cancel()
            raise

    def iterate(self, factory):
        self._check()
        return ManagedStream(self, factory)

    def close(self, timeout=10.0):
        self._check()
        try:
            result = self.call(lambda: self.owner._close(timeout), category="lifecycle")
        except BaseException:
            if self.owner._cleanup_error:
                self._stop()
            raise
        if result.closed:
            self._stop()
        return result

    def exit(self, exc_type, exc, traceback):
        if os.getpid() != self.pid:
            raise ConfigurationError("instrumentation_process_changed")
        if self.stopped:
            if self.owner._cleanup_error:
                raise ConfigurationError("integration_resource_cleanup_failed")
            return
        self._check()
        interrupted = None
        cleanup_error = None
        with self.stream_lock:
            streams = tuple(self.streams)
        for stream in streams:
            try:
                stream.close()
            except KeyboardInterrupt as error:
                interrupted = interrupted or error
            except BaseException as error:
                cleanup_error = cleanup_error or error
        # Context exit owns settlement independently of the waiting caller.
        future = self._submit(lambda: self.owner._exit(exc_type, exc, traceback))
        completed = threading.Event()
        future.add_done_callback(lambda _: completed.set())
        while not completed.is_set():
            try:
                completed.wait()
            except KeyboardInterrupt as error:
                interrupted = interrupted or error
        try:
            error = future.exception()
            if error is not None:
                cleanup_error = cleanup_error or error
        finally:
            self._stop()
        if exc is not None and cleanup_error is not None:
            raise exc.with_traceback(traceback) from cleanup_error
        if interrupted is not None:
            raise interrupted from cleanup_error
        if cleanup_error is not None:
            raise cleanup_error

    def _stop(self):
        if self.stopped:
            return
        self.stopped = True
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread is not None:
            self.thread.join()


class ManagedStream:
    """Demand-driven synchronous iterator holding the owner's execution slot."""

    def __init__(self, bridge, factory):
        self.bridge, self.factory = bridge, factory
        self.iterator = None
        self.closed = False
        self.owned = False
        self.lock = threading.Lock()
        self.pending = None
        self.close_future = None
        self.state_lock = threading.Lock()

    def __iter__(self) -> Self:
        return self

    async def _next(self):
        if self.iterator is None:
            self.iterator = self.factory().__aiter__()
        return await anext(self.iterator)

    def __next__(self) -> Any:
        if os.getpid() != self.bridge.pid:
            raise ConfigurationError("instrumentation_process_changed")
        if self.closed:
            raise StopIteration
        self.bridge._check()
        if not self.lock.acquire(blocking=False):
            raise ConfigurationError("integration_busy")
        try:
            with self.state_lock:
                if self.closed:
                    raise StopIteration
                if not self.owned:
                    if not self.bridge.busy.acquire(blocking=False):
                        raise ConfigurationError("integration_busy")
                    self.owned = True
                    with self.bridge.stream_lock:
                        self.bridge.streams.add(self)
                self.pending = self.bridge._submit(self._next)
                pending = self.pending
            try:
                return pending.result()
            except StopAsyncIteration:
                self.close()
                raise StopIteration from None
            except KeyboardInterrupt:
                pending.cancel()
                self.close()
                raise
            except BaseException:
                self.close()
                raise
            finally:
                with self.state_lock:
                    self.pending = None
        finally:
            self.lock.release()

    async def _drain(self, pending):
        # A cancelled concurrent Future can precede the native iterator's finally.
        # Acquire no second anext until that task has actually stopped.
        while self.iterator is not None and getattr(self.iterator, "ag_running", False):
            await asyncio.sleep(0.001)
        if self.iterator is not None:
            await self.iterator.aclose()

    def close(self) -> None:
        if os.getpid() != self.bridge.pid:
            raise ConfigurationError("instrumentation_process_changed")
        with self.state_lock:
            if self.closed:
                future = self.close_future
            else:
                self.bridge._check()
                self.closed = True
                pending = self.pending
                if pending is not None:
                    pending.cancel()
                if not self.owned:
                    return

                async def finish():
                    try:
                        await self._drain(pending)
                    finally:
                        self.bridge.busy.release()
                        self.owned = False
                        with self.bridge.stream_lock:
                            self.bridge.streams.discard(self)

                # Caller interruption cannot cancel cleanup or release its execution slot.
                self.close_future = future = self.bridge._submit(finish)
        if future is not None:
            if not future.done():
                self.bridge._check()
            future.result()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc):
        self.close()
