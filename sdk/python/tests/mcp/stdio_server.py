"""External MCP fixture: file-controlled discovery and an effect receipt."""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

state_file, receipt_file = map(Path, sys.argv[1:3])
server = Server("sdk-fixture")
session = None


def state():
    return json.loads(state_file.read_text())


@server.list_tools()
async def list_tools(request: types.ListToolsRequest):
    global session
    session = server.request_context.session
    current = state()
    tools = [
        types.Tool(
            name=name,
            description="Record fixture effect",
            inputSchema={
                "type": "object",
                "properties": {"value": {"type": current.get("type", "string")}},
                "required": ["value"],
                "additionalProperties": False,
            },
        )
        for name in current["tools"]
    ]

    if current.get("list_failure"):
        raise ValueError("fixture discovery failure")
    if current.get("notify_during_list"):
        await session.send_tool_list_changed()
        await asyncio.sleep(0.02)
    if current.get("duplicate"):
        tools = tools + tools
    return types.ListToolsResult(
        tools=tools, nextCursor="repeated" if current.get("repeat_cursor") else None
    )


@server.call_tool()
async def call_tool(name, arguments):
    current = state()
    with receipt_file.open("a") as output:
        output.write(
            json.dumps(
                {
                    "name": name,
                    "arguments": arguments,
                    "ambient_secret": os.environ.get("AMBIENT_SDK_SECRET"),
                }
            )
            + "\n"
        )
    while state().get("hold_response"):
        await asyncio.sleep(0.01)
    if current.get("lose_response"):
        os._exit(0)
    return [types.TextContent(type="text", text="effect recorded")]


async def watch():
    previous = state()
    while True:
        await asyncio.sleep(0.02)
        current = state()
        if current != previous:
            previous = current
            if session is not None and current.get("notify", True):
                await session.send_tool_list_changed()


async def main():
    async with stdio_server() as streams:
        monitor = asyncio.create_task(watch())
        try:
            await server.run(*streams, server.create_initialization_options())
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)


async def http_main(port):
    from contextlib import asynccontextmanager

    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    manager = StreamableHTTPSessionManager(server, json_response=True)

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            monitor = asyncio.create_task(watch())
            try:
                yield
            finally:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)

    class Endpoint:
        async def __call__(self, scope, receive, send):
            if scope["method"] == "DELETE":
                await asyncio.sleep(state().get("close_delay", 0))
            if scope["method"] != "GET":
                return await manager.handle_request(scope, receive, send)
            task = asyncio.create_task(manager.handle_request(scope, receive, send))
            try:
                while not task.done():
                    if state().get("drop_get"):
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        await send(
                            {
                                "type": "http.response.body",
                                "body": b"",
                                "more_body": False,
                            }
                        )
                        return
                    await asyncio.sleep(0.01)
                await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    app = Starlette(routes=[Mount("/mcp", app=Endpoint())], lifespan=lifespan)
    await uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical")
    ).serve()


asyncio.run(http_main(int(sys.argv[3])) if len(sys.argv) > 3 else main())
