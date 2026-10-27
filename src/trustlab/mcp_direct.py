# pyright: basic
"""Talk to an MCP server directly, the way any standard MCP client does (path A)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent


class McpDirect:
    """One short-lived MCP session per call, as a connector would use it."""

    def __init__(self, url: str) -> None:
        self.url = url

    def _run(self, work: Any) -> Any:
        async def go() -> Any:
            async with (
                httpx.AsyncClient(timeout=30) as http,
                streamable_http_client(self.url, http_client=http) as (read, write, _),
                ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30)) as session,
            ):
                await session.initialize()
                return await work(session)

        return anyio.run(go)

    def list_tools(self) -> list[dict[str, Any]]:
        async def work(session: ClientSession) -> list[dict[str, Any]]:
            tools = (await session.list_tools()).tools
            return [
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "annotations": tool.annotations.model_dump() if tool.annotations else {},
                    "inputSchema": tool.inputSchema,
                    "outputSchema": tool.outputSchema,
                }
                for tool in tools
            ]

        return self._run(work)

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Return ``{"isError", "structured", "text"}`` exactly as the server answered."""

        async def work(session: ClientSession) -> dict[str, Any]:
            result = await session.call_tool(name, arguments)
            text = "\n".join(
                block.text for block in result.content if isinstance(block, TextContent)
            )
            return {
                "isError": bool(result.isError),
                "structured": result.structuredContent,
                "text": text,
            }

        return self._run(work)
