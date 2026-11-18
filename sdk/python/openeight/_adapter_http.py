"""Owned cancellable HTTP transport for facade calls with total deadlines."""

import asyncio
import threading
import time

import httpx


class DeadlineHTTPTransport:
    """Synchronous bridge to an owned async HTTP pool with total request deadlines."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.client: httpx.AsyncClient | None = None
        self.thread = threading.Thread(
            target=self._run_network_loop, name="openeight-network", daemon=True
        )
        self.thread.start()

    def _run_network_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        finally:
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()

    async def _send_until_deadline(
        self, request: httpx.Request, deadline: float
    ) -> httpx.Response:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        # Native asynchronous I/O can be cancelled even while headers or a body
        # arrive continuously. The caller waits until cancellation has settled.
        async with asyncio.timeout(remaining):
            if self.client is None:
                self.client = httpx.AsyncClient(trust_env=False)
            return await self.client.send(request)

    def send(self, request: httpx.Request, deadline: float) -> httpx.Response:
        """Wait until the response is read or cancelled I/O has settled."""
        return asyncio.run_coroutine_threadsafe(
            self._send_until_deadline(request, deadline), self.loop
        ).result()

    async def _close_connections(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    def close(self) -> None:
        """Close connections on their loop and join the owned network thread."""
        try:
            asyncio.run_coroutine_threadsafe(
                self._close_connections(), self.loop
            ).result()
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join()
