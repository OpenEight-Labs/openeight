"""Full paginated MCP observations and connection lifetime; never execution authority."""

import asyncio
import base64
import hashlib
import json
import threading
import time
from contextlib import AsyncExitStack
from datetime import timedelta
from importlib.metadata import version
from uuid import uuid4

from mcp import ClientSession, types

from openeight._json import snapshot_json
from openeight.errors import ConfigurationError
from openeight.tools import ToolRegistration

from . import _transport


def tool_name(source, remote):
    raw = json.dumps(
        ["openeight-mcp-v1", source.source_id, source.binding_id, remote],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return "mcp_" + base64.b32encode(
        hashlib.sha256(raw).digest()
    ).decode().lower().rstrip("=")


class Source:
    def __init__(self, config, discovery, changed):
        if version("mcp") != "1.30.0":
            raise ConfigurationError("unsupported_framework_version")
        self.config, self.discovery, self.changed = config, discovery, changed
        self.lock = threading.RLock()
        self.revision = 0
        self.epoch = None
        self.fresh_until = 0.0
        self.available = False
        self.descriptors = {}
        self.session = None
        self.task = None
        self.ready = asyncio.Event()
        self.stopping = asyncio.Event()
        self.failed = False
        self.inflight = 0

    def invalidate(self):
        with self.lock:
            self.revision += 1
            self.available = False
        self.changed.set()

    async def _message(self, message):
        if isinstance(message, Exception):
            self.failed = True
            self.invalidate()
        elif isinstance(message, types.ServerNotification) and isinstance(
            message.root, types.ToolListChangedNotification
        ):
            self.invalidate()

    async def _logging(self, params):
        pass

    async def _run(self):
        try:
            async with AsyncExitStack() as stack:
                transport = (
                    _transport.http(
                        self.config, self.disconnect, self.discovery.request_timeout
                    )
                    if self.config.transport == "http"
                    else _transport.stdio(self.config, self.disconnect)
                )
                streams = await stack.enter_async_context(transport)
                session = await stack.enter_async_context(
                    ClientSession(
                        *streams,
                        read_timeout_seconds=timedelta(
                            seconds=self.discovery.request_timeout
                        ),
                        message_handler=self._message,
                        logging_callback=self._logging,
                    )
                )
                result = await session.initialize()
                if (
                    result.protocolVersion != "2025-11-25"
                    or result.capabilities.tools is None
                ):
                    raise ConfigurationError("unsupported_framework_version")
                self.session = session
                self.epoch = str(uuid4())
                self.ready.set()
                await self.stopping.wait()
        except asyncio.CancelledError:
            raise
        except BaseException:
            self.failed = True
        finally:
            self.session = None
            self.invalidate()
            self.ready.set()

    def disconnect(self):
        self.failed = True
        self.invalidate()

    async def connect(self):
        if self.task is not None:
            if self.inflight:
                raise ConfigurationError("tool_source_unavailable")
            await self.close()
        self.ready, self.stopping = asyncio.Event(), asyncio.Event()
        self.failed = False
        self.task = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self.ready.wait(), self.discovery.request_timeout)
        except TimeoutError:
            await self.close()
            raise ConfigurationError("tool_source_unavailable") from None
        if self.failed or self.session is None:
            await self.close()
            raise ConfigurationError("tool_source_unavailable")

    async def listing(self):
        if self.failed or self.session is None:
            await self.connect()
        with self.lock:
            revision, epoch = self.revision, self.epoch
        tools, cursors, cursor, size = {}, set(), None, 0
        try:
            async with asyncio.timeout(self.discovery.request_timeout):
                for _ in range(self.discovery.max_pages):
                    result = await self.session.list_tools(cursor=cursor)
                    for descriptor in result.tools:
                        if descriptor.name in tools or not descriptor.name:
                            raise ConfigurationError("unsupported_tool_definition")
                        execution = getattr(descriptor, "execution", None)
                        if (
                            execution is not None
                            and getattr(execution, "taskSupport", None) == "required"
                        ):
                            raise ConfigurationError("unsupported_tool_definition")
                        fact = ToolRegistration(
                            name=tool_name(self.config, descriptor.name),
                            description=descriptor.description or "",
                            input_schema=descriptor.inputSchema,
                        )
                        raw = snapshot_json(
                            descriptor.model_dump(mode="json", exclude_none=True),
                            "invalid_event",
                        )
                        size += len(raw)
                        tools[descriptor.name] = (fact, raw)
                        if (
                            len(tools) > self.discovery.max_tools
                            or size > self.discovery.max_catalog_bytes
                        ):
                            raise ConfigurationError("tool_discovery_limit_exceeded")
                    cursor = result.nextCursor
                    if cursor is None:
                        break
                    if cursor in cursors:
                        raise ConfigurationError("unsupported_tool_definition")
                    cursors.add(cursor)
                else:
                    raise ConfigurationError("tool_discovery_limit_exceeded")
        except asyncio.CancelledError:
            raise
        except ConfigurationError:
            self.invalidate()
            raise
        except Exception:
            self.disconnect()
            raise ConfigurationError("tool_source_unavailable") from None
        with self.lock:
            if revision != self.revision or epoch != self.epoch or self.failed:
                raise ConfigurationError("tool_catalog_stale")
            self.descriptors = tools
            self.fresh_until = time.monotonic() + self.discovery.refresh_interval
            self.available = False
        return revision, epoch, tools

    def validate(self, epoch):
        with self.lock:
            if self.failed or self.session is None or epoch != self.epoch:
                raise ConfigurationError("tool_source_unavailable")
            if not self.available or time.monotonic() >= self.fresh_until:
                raise ConfigurationError("tool_catalog_stale")

    async def call(self, remote_name, args, session):
        try:
            # Exactly one RPC. No high-level call_tool cache refresh or automatic replay.
            result = await session.send_request(
                types.ClientRequest(
                    types.CallToolRequest(
                        params=types.CallToolRequestParams(
                            name=remote_name, arguments=args
                        )
                    )
                ),
                types.CallToolResult,
                request_read_timeout_seconds=timedelta(
                    seconds=self.discovery.request_timeout
                ),
            )
            raw = result.model_dump(mode="json", exclude_none=True)
            if (
                len(snapshot_json(raw, "invalid_event"))
                > self.discovery.max_catalog_bytes
            ):
                raise ConfigurationError("invalid_tool_result")
            return raw
        except asyncio.CancelledError:
            raise
        except ConfigurationError:
            raise
        except Exception:
            self.disconnect()
            raise ConfigurationError("tool_source_unavailable") from None

    async def close(self):
        while self.inflight:
            await asyncio.sleep(0.01)
        self.stopping.set()
        if self.task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(self.task), self.discovery.request_timeout
                )
            except TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
