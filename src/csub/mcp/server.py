"""MCP stdio server exposing csub_* tools. Built on the lowlevel ``mcp`` Server so that the
hand-written JSON schemas from :mod:`csub.protocol` are what the agent sees."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import anyio
import jsonschema
from mcp import types
from mcp.server.lowlevel import Server

from csub import __version__
from csub.client.api import Client
from csub.mcp.tools import INSTRUCTIONS, TOOLS, ToolDef
from csub.protocol import CsubError


def _error_result(code: str, message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"{code}: {message}")],
        structuredContent={"error": {"code": code, "message": message}},
        isError=True,
    )


def _tool_listing(t: ToolDef) -> types.Tool:
    return types.Tool(
        name=t.name,
        description=t.description,
        inputSchema=t.input_schema,
        annotations=types.ToolAnnotations(
            readOnlyHint=t.read_only,
            destructiveHint=t.destructive,
            idempotentHint=t.idempotent,
            openWorldHint=False,
        ),
    )


def build_server(client_factory: Callable[[], Client] = Client.from_env) -> Server[Any]:
    client: Client | None = None

    def get_client() -> Client:
        nonlocal client
        if client is None:
            client = client_factory()
        return client

    async def on_list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[_tool_listing(t) for t in TOOLS.values()])

    async def on_call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        tool = TOOLS.get(params.name)
        if tool is None:
            return _error_result("invalid_request", f"unknown tool {params.name}")
        args = dict(params.arguments or {})
        try:
            jsonschema.validate(args, tool.input_schema)
        except jsonschema.ValidationError as e:
            where = "/".join(str(p) for p in e.absolute_path) or "arguments"
            return _error_result("invalid_request", f"{where}: {e.message}")
        try:
            result = await anyio.to_thread.run_sync(tool.handler, get_client(), args)
        except CsubError as e:
            return _error_result(e.code, e.message)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result, indent=1))],
            structuredContent=result,
        )

    return Server(
        "csub",
        version=__version__,
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def _serve_stdio() -> None:
    from mcp.server.stdio import stdio_server

    server = build_server()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> int:
    anyio.run(_serve_stdio)
    return 0
