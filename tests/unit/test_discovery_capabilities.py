"""Discovery asks a server only for what it offers.

Found on a real deployment: Hugging Face's MCP server has no prompts and answers prompts/list
with an error, and `muse-vm add-server huggingface` failed although its tools were fine.
"""

from typing import Any

import anyio
import pytest
from mcp import types
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import McpError

from muse_mcp_adapter.domain.models import ServerRegistration
from muse_mcp_adapter.transport.sdk_client import (
    InMemorySessionFactory,
    McpSdkClient,
    _listing,  # pyright: ignore[reportPrivateUsage]
)


def _registration() -> ServerRegistration:
    return ServerRegistration(
        server_id="tools-only",
        display_name="Tools only",
        endpoint="https://mcp.example.test/mcp",
        auth_profile="none",
        provider_pack=None,
        enabled=True,
        created_at="2026-10-19T18:00:00Z",
        updated_at="2026-10-19T18:00:00Z",
    )


def _tools_only() -> Server[Any, Any]:
    server: Server[Any, Any] = Server("tools only")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:  # pyright: ignore[reportUnusedFunction]
        return [
            types.Tool(
                name="lookup",
                description="Look something up.",
                inputSchema={"type": "object", "properties": {}},
                annotations=types.ToolAnnotations(readOnlyHint=True),
            )
        ]

    return server


def test_a_server_with_only_tools_is_discovered() -> None:
    snapshot = McpSdkClient(InMemorySessionFactory(_tools_only())).discover(_registration())

    assert [(item["type"], item["name"]) for item in snapshot.primitives] == [("tool", "lookup")]
    assert snapshot.capabilities["prompts"] is False
    assert snapshot.capabilities["resources"] is False


def test_what_the_server_does_not_declare_is_not_asked_for() -> None:
    asked: list[str | None] = []

    async def prompts(cursor: str | None) -> Any:
        asked.append(cursor)
        raise AssertionError("prompts/list must not be sent")

    only_tools: dict[str, object] = {"tools": {}}
    found = anyio.run(_listing, prompts, "prompts", only_tools, "prompts")

    assert found == []
    assert asked == []


def test_method_not_found_is_an_empty_list_and_other_errors_still_fail() -> None:
    async def not_found(cursor: str | None) -> Any:
        raise McpError(types.ErrorData(code=types.METHOD_NOT_FOUND, message="not supported"))

    async def broken(cursor: str | None) -> Any:
        raise McpError(types.ErrorData(code=types.INTERNAL_ERROR, message="boom"))

    with_prompts: dict[str, object] = {"prompts": {}}
    assert anyio.run(_listing, not_found, "prompts", with_prompts, "prompts") == []
    with pytest.raises(McpError):
        anyio.run(_listing, broken, "prompts", with_prompts, "prompts")
