# pyright: basic
"""Talk to an MCP server directly, the way any standard MCP client would.

This is the baseline for every parity check: what the server itself answers when no connector
is in between.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, TypeVar, cast

import anyio
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from pydantic import AnyUrl

T = TypeVar("T")


class Direct:
    """One short-lived MCP session per call, with optional bearer authentication."""

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        elicit: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self.url = url
        self._headers = headers or {}
        self._elicit = elicit
        self.logs: list[tuple[str, str]] = []
        self.progress: list[tuple[float, float | None, str | None]] = []
        self.initialize_result: types.InitializeResult | None = None

    async def _session(self) -> Any:
        raise NotImplementedError

    def run(self, work: Callable[[ClientSession], Awaitable[T]]) -> T:
        async def go() -> T:
            import httpx

            async with (
                httpx.AsyncClient(headers=self._headers, timeout=30) as http,
                streamable_http_client(self.url, http_client=http) as (read, write, _),
                ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=30),
                    elicitation_callback=cast(Any, self._elicitation) if self._elicit else None,
                    logging_callback=self._log,
                ) as session,
            ):
                self.initialize_result = await session.initialize()
                return await work(session)

        return anyio.run(go)

    async def _log(self, params: types.LoggingMessageNotificationParams) -> None:
        self.logs.append((str(params.level), str(params.data)))

    async def _on_progress(self, progress: float, total: float | None, message: str | None) -> None:
        self.progress.append((progress, total, message))

    async def _elicitation(self, _context: Any, params: Any) -> types.ElicitResult:
        assert self._elicit is not None
        answer = self._elicit(str(getattr(params, "message", "")))
        if answer is None:
            return types.ElicitResult(action="decline")
        return types.ElicitResult(action="accept", content=answer)

    # --- conveniences -------------------------------------------------------------------

    def list_tools(self) -> list[types.Tool]:
        async def work(session: ClientSession) -> list[types.Tool]:
            return list((await session.list_tools()).tools)

        return self.run(work)

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> types.CallToolResult:
        async def work(session: ClientSession) -> types.CallToolResult:
            return await session.call_tool(
                name, arguments or {}, progress_callback=self._on_progress
            )

        return self.run(work)

    def ping(self) -> bool:
        async def work(session: ClientSession) -> bool:
            await session.send_ping()
            return True

        return self.run(work)

    def read(self, uri: str) -> types.ReadResourceResult:
        async def work(session: ClientSession) -> types.ReadResourceResult:
            return await session.read_resource(AnyUrl(uri))

        return self.run(work)

    def prompt(self, name: str, arguments: dict[str, str] | None = None) -> types.GetPromptResult:
        async def work(session: ClientSession) -> types.GetPromptResult:
            return await session.get_prompt(name, arguments or {})

        return self.run(work)

    def snapshot(self) -> dict[str, Any]:
        """Everything a client can enumerate."""

        async def work(session: ClientSession) -> dict[str, Any]:
            return {
                "tools": list((await session.list_tools()).tools),
                "resources": list((await session.list_resources()).resources),
                "templates": list((await session.list_resource_templates()).resourceTemplates),
                "prompts": list((await session.list_prompts()).prompts),
                "server": self.initialize_result,
            }

        return self.run(work)
