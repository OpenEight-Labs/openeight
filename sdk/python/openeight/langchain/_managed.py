"""Reusable ordinary facade; durable agents retain explicit session ownership."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import AsyncGenerator, Sequence
from dataclasses import replace
from types import MappingProxyType, TracebackType
from typing import TYPE_CHECKING, Any, Self

from langchain_core.runnables import RunnableConfig
from langgraph.types import StreamMode

from openeight._managed_types import CatalogReadiness, CatalogSnapshot, CatalogStatus
from openeight._validation import validate_timeout
from openeight.compatibility import CompatibilityReport
from openeight.delivery import DeliveryStatus
from openeight.errors import (
    CleanupIncompleteError,
    ConfigurationError,
    ResourceCleanupError,
)
from openeight.lifecycle import LifecycleStatus, OperationStatus

from ._session import (  # noqa: F401
    _OUTCOMES,
    DurableAgent,
    LocalDurableAgent,
    _SessionOwner,
    safe_diagnostics,
)

if TYPE_CHECKING:
    from ._sync import ManagedStream


def make_managed_agent(*, session_factory, inspect_compatibility, checkpointed):
    return ManagedAgent(
        session_factory=session_factory,
        inspect_compatibility=inspect_compatibility,
        checkpointed=checkpointed,
    )


class ManagedAgent:
    """Reusable agent owner; keep it open for streaming, readiness and tool refresh."""

    def __init__(self, *, session_factory, inspect_compatibility, checkpointed):
        self._factory = session_factory
        self._inspect = inspect_compatibility
        self._checkpointed = checkpointed
        self._lock = threading.RLock()
        self._pid = os.getpid()
        self._mode = self._loop = self._session = None
        self._context_owned = False
        self._starting = False
        self._close_requested = False
        self._state = "idle"
        self._active = 0
        self._error = None
        self._outcomes = ()
        self._catalog = CatalogSnapshot(0, (), (), (), "unknown", False)
        self._catalog_state = CatalogStatus(
            0, (), MappingProxyType({"catalog_delivery_state": "not_started"}), None
        )
        self._delivery = DeliveryStatus()

    def _check_process(self):
        if self._pid != os.getpid():
            raise ConfigurationError("instrumentation_process_changed")

    def _check(self, mode):
        self._check_process()
        if self._mode is not None and self._mode != mode:
            raise ConfigurationError("integration_mode_mismatch")
        if mode == "sync":
            from ._runtime import CURRENT_INVOCATION

            invocation = CURRENT_INVOCATION.get()
            if (
                invocation is not None
                and self._session is not None
                and invocation.owner is self._session._owner
            ):
                raise ConfigurationError("integration_sync_reentry")
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise ConfigurationError("integration_sync_reentry")
        loop = asyncio.get_running_loop() if mode == "async" else None
        if mode == "async" and self._loop is not None and self._loop is not loop:
            raise ConfigurationError("integration_loop_changed")
        return loop

    def _activate(self, mode, *, transient=False, context=False):
        loop = self._check(mode)
        with self._lock:
            if self._state == "closed":
                raise ConfigurationError("integration_closed")
            if self._state == "open":
                if context:
                    raise ConfigurationError("integration_busy")
                return self._session, False
            if self._state != "idle":
                raise ConfigurationError("integration_busy")
            self._mode, self._loop = mode, loop
            self._state = "executing-transient" if transient else "opening"
            self._context_owned = context
            self._starting = True
            try:
                session = self._factory()
            except BaseException:
                self._starting = False
                self._context_owned = False
                self._state = "idle"
                raise
            self._session = session
            return session, True

    def _snapshot(self, session):
        if session._owner is None:
            self._delivery = session._cleanup_status or DeliveryStatus(
                closed=session._closed
            )
        if session._owner is not None:
            self._delivery = session.delivery_status()
            # Catalog facts are immutable snapshots retained after transport shutdown.
            owner = session._owner
            with owner.lock:
                self._catalog = CatalogSnapshot(
                    session._coordinator.current.number if session._coordinator else 1,
                    owner.catalog.facts,
                    owner.catalog_delivery.snapshot(),
                    owner.catalog.snapshot(),
                    "complete",
                    not owner._defer_catalog
                    and len(owner.catalog_delivery.snapshot())
                    == len(owner.catalog.facts)
                    and owner.catalog_delivery.diagnostics()["catalog_delivery_state"]
                    in {"acknowledged", "empty"},
                )
            self._catalog_state = session._catalog_status(settled=True)

    def _operation(self, phase):
        current = _OUTCOMES.get()
        outcomes = tuple(current) if current is not None else self._outcomes
        from openeight.lifecycle import ExecutionOutcome

        session = self._session
        owner = session._owner if session else None
        if owner is not None:
            with owner.lock:
                roots = tuple(owner.roots)
            known = {item.run_id for item in outcomes}
            outcomes = (
                *outcomes,
                *(
                    ExecutionOutcome(
                        item_index=len(outcomes) + index,
                        run_id=invocation.run_id,
                        state="unsettled",
                    )
                    for index, invocation in enumerate(roots)
                    if invocation.run_id not in known
                ),
            )
        return OperationStatus(
            phase=phase,
            run_ids=tuple(item.run_id for item in outcomes if item.run_id is not None),
            outcomes=outcomes,
        )

    def _cleanup_failed(self, session, error):
        self._snapshot(session)
        with self._lock:
            if session._cleanup_error:
                self._state = "closed"
                self._error = ResourceCleanupError(operation=self._operation("cleanup"))
            else:
                self._state = "settling"
                self._error = CleanupIncompleteError(
                    operation=self._operation("settlement")
                )
        return self._error

    def _entered(self, session, transient):
        self._snapshot(session)
        with self._lock:
            self._starting = False
            self._state = "executing-transient" if transient else "open"

    def _startup_failed(self, session):
        self._snapshot(session)
        with self._lock:
            self._starting = False
            if session._cleanup_error:
                self._state = "closed"
                self._error = ResourceCleanupError(operation=self._operation("startup"))
            elif session._closed or session._owner is None:
                self._state = "idle"
                self._session = None
                self._context_owned = False
            else:
                self._state = "settling"
                self._error = CleanupIncompleteError(
                    operation=self._operation("startup")
                )

    def open(self):
        """Open a persistent synchronous session and return this owner."""

        session, fresh = self._activate("sync")
        if fresh:
            try:
                session.__enter__()
                self._entered(session, False)
            except BaseException:
                self._startup_failed(session)
                raise
        return self

    async def aopen(self):
        """Open a persistent asynchronous session on the current event loop."""

        session, fresh = self._activate("async")
        if fresh:
            try:
                await session.__aenter__()
                self._entered(session, False)
            except BaseException:
                self._startup_failed(session)
                raise
        return self

    def __enter__(self) -> Self:
        session, _ = self._activate("sync", context=True)
        try:
            session.__enter__()
            self._entered(session, False)
            return self
        except BaseException:
            self._startup_failed(session)
            raise

    async def __aenter__(self) -> Self:
        session, _ = self._activate("async", context=True)
        try:
            await session.__aenter__()
            self._entered(session, False)
            return self
        except BaseException:
            self._startup_failed(session)
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._check("sync")
        if self._session is None:
            self._before_close("sync", 10.0)
            return
        try:
            self._session.__exit__(exc_type, exc, traceback)
        except BaseException as error:
            if self._session._closed and not self._session._cleanup_error:
                if exc is None:
                    raise
                return
            secondary = self._cleanup_failed(self._session, error)
            if exc is None:
                raise secondary from None
        finally:
            self._snapshot(self._session)
            with self._lock:
                self._state = "closed"
                self._context_owned = False

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._check("async")
        if self._session is None:
            self._before_close("async", 10.0)
            return
        try:
            await self._session.__aexit__(exc_type, exc, traceback)
        except BaseException as error:
            if self._session._closed and not self._session._cleanup_error:
                if exc is None:
                    raise
                return
            secondary = self._cleanup_failed(self._session, error)
            if exc is None:
                raise secondary from None
        finally:
            self._snapshot(self._session)
            with self._lock:
                self._state = "closed"
                self._context_owned = False

    def _completed_close(self, session, status, *, transient=False):
        self._snapshot(session)
        with self._lock:
            if not status.closed:
                self._state = "settling"
                self._error = CleanupIncompleteError(
                    operation=self._operation("settlement")
                )
                raise self._error
            if transient and self._close_requested:
                if self._state != "closed":
                    self._state = "settling"
                return status
            self._state = "idle" if transient else "closed"
            if transient:
                self._session = None
            self._error = None
        return status

    def _before_close(self, mode, timeout):
        validate_timeout(timeout)
        self._check(mode)
        with self._lock:
            if self._state == "closed":
                if self._error is not None:
                    raise self._error
                return None
            if self._state == "idle":
                self._state = "closed"
                self._delivery = replace(self._delivery, closed=True)
                return None
            if self._starting or self._state == "opening":
                raise ConfigurationError("integration_busy")
            self._close_requested = True
            self._state = "settling"
            return self._session

    def close(self, timeout: float = 10.0) -> DeliveryStatus:
        """Seal new work and return delivery status; incomplete cleanup raises an SDK error."""

        session = self._before_close("sync", timeout)
        if session is None:
            return self._delivery
        try:
            result = session.close(timeout)
        except BaseException as error:
            raise self._cleanup_failed(session, error) from None
        return self._completed_close(session, result)

    async def aclose(self, timeout: float = 10.0) -> DeliveryStatus:
        """Close asynchronously while retaining ownership of unfinished cleanup."""

        session = self._before_close("async", timeout)
        if session is None:
            return self._delivery
        try:
            result = await session.aclose(timeout)
        except asyncio.CancelledError:
            self._cleanup_failed(session, None)
            raise
        except BaseException as error:
            raise self._cleanup_failed(session, error) from None
        return self._completed_close(session, result)

    def _admit_call(self, mode, *, publish=False):
        self._check(mode)
        with self._lock:
            if self._checkpointed and self._state == "idle" and not publish:
                raise ConfigurationError("integration_not_entered")
            session, fresh = self._activate(mode, transient=True)
            self._active += 1
            return session, fresh

    def _call(self, method, *args, **kwargs):
        session, fresh = self._admit_call("sync", publish=method == "publish")
        primary = None
        outcomes = []
        outcome_token = _OUTCOMES.set(outcomes)
        try:
            if fresh:
                try:
                    session.__enter__()
                    self._entered(session, True)
                except BaseException:
                    self._startup_failed(session)
                    raise
            return (
                session.catalog_snapshot()
                if method == "publish"
                else getattr(session, method)(*args, **kwargs)
            )
        except BaseException as error:
            primary = error
            raise
        finally:
            with self._lock:
                self._active -= 1
                self._outcomes = outcomes
            _OUTCOMES.reset(outcome_token)
            if fresh and session._owner is not None and self._state != "idle":
                try:
                    self._completed_close(session, session.close(), transient=True)
                except BaseException as error:
                    if not isinstance(error, CleanupIncompleteError):
                        error = self._cleanup_failed(session, error)
                    if primary is None:
                        raise error from None

    async def _acall(self, method, *args, **kwargs):
        session, fresh = self._admit_call("async", publish=method == "publish")
        primary = None
        outcomes = []
        outcome_token = _OUTCOMES.set(outcomes)
        try:
            if fresh:
                try:
                    await session.__aenter__()
                    self._entered(session, True)
                except BaseException:
                    self._startup_failed(session)
                    raise
            return (
                session.catalog_snapshot()
                if method == "publish"
                else await getattr(session, method)(*args, **kwargs)
            )
        except BaseException as error:
            primary = error
            raise
        finally:
            with self._lock:
                self._active -= 1
                self._outcomes = outcomes
            _OUTCOMES.reset(outcome_token)
            if fresh and session._owner is not None and self._state != "idle":
                try:
                    self._completed_close(
                        session, await session.aclose(), transient=True
                    )
                except BaseException as error:
                    if not isinstance(error, CleanupIncompleteError):
                        error = self._cleanup_failed(session, error)
                    if primary is None:
                        raise error from None

    def invoke(
        self, input: Any, config: RunnableConfig | None = None, *, context: Any = None
    ) -> Any:
        """Execute one input; idle non-checkpointed owners use a temporary session."""

        return self._call("invoke", input, config, context=context)

    async def ainvoke(
        self, input: Any, config: RunnableConfig | None = None, *, context: Any = None
    ) -> Any:
        """Execute asynchronously; checkpointed owners require an explicit open session."""

        return await self._acall("ainvoke", input, config, context=context)

    def batch(
        self,
        inputs: Sequence[Any],
        config: RunnableConfig | list[RunnableConfig] | None = None,
        *,
        return_exceptions: bool = False,
        context: Any = None,
    ) -> list[Any]:
        return self._call(
            "batch",
            inputs,
            config,
            return_exceptions=return_exceptions,
            context=context,
        )

    async def abatch(
        self,
        inputs: Sequence[Any],
        config: RunnableConfig | list[RunnableConfig] | None = None,
        *,
        return_exceptions: bool = False,
        context: Any = None,
    ) -> list[Any]:
        return await self._acall(
            "abatch",
            inputs,
            config,
            return_exceptions=return_exceptions,
            context=context,
        )

    def _persistent(self, mode):
        self._check(mode)
        with self._lock:
            if self._state == "closed":
                raise ConfigurationError("integration_closed")
            if self._state != "open":
                raise ConfigurationError(
                    "integration_not_entered"
                    if self._state == "idle"
                    else "integration_busy"
                )
            return self._session

    def stream(
        self,
        input: Any,
        config: RunnableConfig | None = None,
        *,
        context: Any = None,
        stream_mode: StreamMode | list[StreamMode] = "values",
        subgraphs: bool = False,
    ) -> ManagedStream:
        """Stream from an open session; close the iterator when stopping early."""

        return self._persistent("sync").stream(
            input, config, context=context, stream_mode=stream_mode, subgraphs=subgraphs
        )

    async def astream(
        self,
        input: Any,
        config: RunnableConfig | None = None,
        *,
        context: Any = None,
        stream_mode: StreamMode | list[StreamMode] = "values",
        subgraphs: bool = False,
    ) -> AsyncGenerator[Any, None]:
        """Stream asynchronously from an open session and close the native iterator on exit."""

        iterator = self._persistent("async").astream(
            input, config, context=context, stream_mode=stream_mode, subgraphs=subgraphs
        )
        try:
            async for item in iterator:
                yield item
        finally:
            await iterator.aclose()

    def publish(self) -> CatalogSnapshot:
        """Publish tool facts without granting policy or execution authority."""

        return self._call("publish")

    async def apublish(self) -> CatalogSnapshot:
        """Publish tool facts asynchronously without authorizing execution."""

        return await self._acall("publish")

    def validate(self) -> CompatibilityReport:
        """Inspect local compatibility without opening an execution session."""

        self._check_process()
        return self._inspect()

    async def avalidate(self) -> CompatibilityReport:
        return self.validate()

    def lifecycle_status(self) -> LifecycleStatus:
        """Snapshot local execution ownership and cleanup state without waiting."""

        self._check_process()
        with self._lock:
            session = self._session
            owner = session._owner if session else None
            active = 0
            if owner is not None:
                with owner.lock:
                    for item in tuple(owner.roots):
                        with item.lock:
                            active += bool(item.handlers or not item.root_ended)
            return LifecycleStatus(
                state=self._state,
                cleanup_complete=self._state in {"idle", "closed"}
                and self._error is None,
                effects_settled=active == 0,
                cleanup_terminal=bool(session and session._cleanup_error),
                error_code=self._error.code if self._error else None,
                active_executions=active,
            )

    def catalog_snapshot(self) -> CatalogSnapshot:
        """Return current or retained immutable catalog facts without refreshing them."""

        self._check_process()
        if self._state == "open":
            return self._session.catalog_snapshot()
        return self._catalog

    def catalog_status(self) -> CatalogStatus:
        """Return discovery and publication status without refreshing the catalog."""

        self._check_process()
        if self._state == "open":
            return self._session.catalog_status()
        return self._catalog_state

    def delivery_status(self) -> DeliveryStatus:
        """Return local delivery accounting; queued work is not an acknowledgement."""

        self._check_process()
        if self._session is not None and self._session._owner is not None:
            return self._session.delivery_status()
        return self._delivery

    def readiness(self, timeout: float = 10.0) -> CatalogReadiness:
        """Fetch server readiness through an open session; timeout is in seconds."""

        return self._persistent("sync").readiness(timeout)

    async def areadiness(self, timeout: float = 10.0) -> CatalogReadiness:
        """Fetch server readiness asynchronously through an open session."""

        return await self._persistent("async").areadiness(timeout)

    def refresh_tools(self, timeout: float = 10.0) -> CatalogStatus:
        """Refresh discovered tools through an open session; timeout is in seconds."""

        return self._persistent("sync").refresh_tools(timeout)

    async def arefresh_tools(self, timeout: float = 10.0) -> CatalogStatus:
        """Refresh discovered tools asynchronously through an open session."""

        return await self._persistent("async").arefresh_tools(timeout)

    def flush(self, timeout: float = 10.0) -> DeliveryStatus:
        validate_timeout(timeout)
        self._check("sync")
        return (
            self._persistent("sync").flush(timeout)
            if self._state == "open"
            else self.delivery_status()
        )

    async def aflush(self, timeout: float = 10.0) -> DeliveryStatus:
        validate_timeout(timeout)
        self._check("async")
        return (
            await self._persistent("async").aflush(timeout)
            if self._state == "open"
            else self.delivery_status()
        )

    def new_thread(self) -> str:
        return self._persistent(self._mode or "sync").new_thread()

    def release_thread(self, thread_id: str) -> None:
        return self._persistent(self._mode or "sync").release_thread(thread_id)
