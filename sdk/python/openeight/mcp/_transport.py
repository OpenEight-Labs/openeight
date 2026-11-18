"""Owned MCP transports with explicit process environment and safe diagnostics."""

import asyncio
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar

import anyio
import httpx
from mcp import types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.message import SessionMessage

from openeight.errors import ConfigurationError

_OWNED = ContextVar("openeight_mcp_transport", default=False)


class _OwnedDiagnostics(logging.Filter):
    def filter(self, record):
        # Suppress transport payloads only in SDK-owned tasks, not other MCP clients.
        return not _OWNED.get()


_FILTER = _OwnedDiagnostics()
for _name in (
    "mcp.client.streamable_http",
    "mcp.client.session",
    "mcp.shared.session",
    "httpx",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
):
    logging.getLogger(_name).addFilter(_FILTER)


@asynccontextmanager
async def stdio(source, invalidate):
    """Frame official MCP messages over a child with an exact environment.

    The upstream stdio convenience helper merges ambient OS variables. The SDK
    owns only process/framing here; negotiation and RPC remain ClientSession's.
    """
    token = _OWNED.set(True)
    process = None
    tasks = []
    incoming, reader = anyio.create_memory_object_stream(0)
    writer, outgoing = anyio.create_memory_object_stream(0)
    try:
        process = await asyncio.create_subprocess_exec(
            source.command,
            *source.args,
            env=dict(source.env),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=1048576,
        )

        async def read():
            try:
                while line := await process.stdout.readline():
                    if len(line) > 1048576:
                        raise ValueError
                    await incoming.send(
                        SessionMessage(types.JSONRPCMessage.model_validate_json(line))
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                invalidate()
                await incoming.aclose()

        async def write():
            try:
                async for message in outgoing:
                    raw = (
                        message.message.model_dump_json(
                            by_alias=True, exclude_none=True
                        ).encode()
                        + b"\n"
                    )
                    process.stdin.write(raw)
                    await process.stdin.drain()
            except asyncio.CancelledError:
                raise
            except Exception:
                invalidate()
            finally:
                process.stdin.close()

        tasks = [asyncio.create_task(read()), asyncio.create_task(write())]
        yield reader, writer
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                process.kill()
                await process.wait()
        await reader.aclose()
        await writer.aclose()
        await incoming.aclose()
        await outgoing.aclose()
        _OWNED.reset(token)


class _Stream(httpx.AsyncByteStream):
    def __init__(self, base, invalidate, watch_end, *, sse=False):
        self.base, self.invalidate, self.watch_end = base, invalidate, watch_end
        self.sse = sse

    async def __aiter__(self):
        size, line_size, after_cr = 0, 0, False
        try:
            async for chunk in self.base:
                if self.sse:
                    # Follow SSE's CRLF, CR and LF line semantics across reads.
                    # The optional LF after CR belongs to the same logical line.
                    for byte in chunk:
                        if after_cr and byte == 10:
                            if size:
                                size += 1
                                if size > 1048576:
                                    raise ValueError
                            after_cr = False
                            continue
                        after_cr = False
                        size += 1
                        if size > 1048576:
                            raise ValueError
                        if byte in (10, 13):
                            if line_size == 0:
                                size = 0
                            line_size = 0
                            after_cr = byte == 13
                        else:
                            line_size += 1
                else:
                    size += len(chunk)
                    if size > 1048576:
                        raise ValueError
                yield chunk
        except Exception:
            self.invalidate()
            raise ConfigurationError("tool_source_unavailable") from None
        finally:
            if self.watch_end:
                self.invalidate()

    async def aclose(self):
        await self.base.aclose()


class _HTTPTransport(httpx.AsyncBaseTransport):
    def __init__(self, invalidate):
        self.base = httpx.AsyncHTTPTransport(retries=0)
        self.invalidate = invalidate

    async def handle_async_request(self, request):
        try:
            response = await self.base.handle_async_request(request)
        except Exception:
            self.invalidate()
            raise ConfigurationError("tool_source_unavailable") from None
        if response.status_code == 401 or 300 <= response.status_code < 400:
            self.invalidate()
            await response.aclose()
            raise ConfigurationError("tool_source_unavailable") from None
        if response.headers.get("content-encoding", "").strip().lower() not in {
            "",
            "identity",
        }:
            self.invalidate()
            await response.aclose()
            raise ConfigurationError("tool_source_unavailable") from None
        response.stream = _Stream(
            response.stream,
            self.invalidate,
            request.method == "GET" and response.status_code == 200,
            sse=response.headers.get("content-type", "").split(";", 1)[0]
            == "text/event-stream",
        )
        return response

    async def aclose(self):
        await self.base.aclose()


@asynccontextmanager
async def http(source, invalidate, timeout):
    token = _OWNED.set(True)

    async def headers(request):
        values = await source.headers() if callable(source.headers) else source.headers
        if not hasattr(values, "items") or any(
            type(k) is not str or type(v) is not str or "\r" in k + v or "\n" in k + v
            for k, v in values.items()
        ):
            raise ConfigurationError("invalid_integration_configuration")
        request.headers.update(values)
        request.headers["Accept-Encoding"] = "identity"

    try:
        async with httpx.AsyncClient(
            transport=_HTTPTransport(invalidate),
            follow_redirects=False,
            timeout=timeout,
            trust_env=False,
            event_hooks={"request": [headers]},
        ) as client:
            async with streamable_http_client(
                source.url, http_client=client
            ) as streams:
                yield streams[:2]
    finally:
        _OWNED.reset(token)
