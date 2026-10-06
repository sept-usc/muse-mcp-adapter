"""Fault injection at the HTTP layer, for the failures a tool function cannot cause itself."""

from __future__ import annotations

import json
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .state import AtlasState


class ChaosMiddleware:
    """Drops the HTTP response of a tool call, before or after the tool has run.

    Dropping *after* the tool ran is the case that matters: the side effect happened, but the
    caller never learns the outcome. That is exactly what the adapter must treat as unknown.
    """

    def __init__(self, app: ASGIApp, state: AtlasState, path: str = "/mcp") -> None:
        self.app = app
        self._state = state
        self._path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != self._path or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        body = await _read_body(receive)
        tool = _tool_name(body)
        replay = _replay(body, receive)
        if tool and self._state.should_drop(tool, after_effect=False):
            await _abort(send)
            return
        if tool and self._state.should_drop(tool, after_effect=True):
            captured: list[Message] = []

            async def capture(message: Message) -> None:
                captured.append(message)

            await self.app(scope, replay, capture)  # the tool runs and its effect is recorded
            await _abort(send)
            return
        await self.app(scope, replay, send)


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(chunks)


def _replay(body: bytes, original: Receive) -> Receive:
    """Serve the buffered body once, then defer to the real connection (so disconnects work)."""
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if sent:
            return await original()
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _tool_name(body: bytes) -> str | None:
    try:
        parsed: Any = json.loads(body)
    except ValueError:
        return None
    if isinstance(parsed, dict) and parsed.get("method") == "tools/call":  # type: ignore[union-attr]
        params = parsed.get("params")  # type: ignore[union-attr]
        if isinstance(params, dict):
            name = params.get("name")  # type: ignore[union-attr]
            return name if isinstance(name, str) else None
    return None


async def _abort(send: Send) -> None:
    """Start a response, send part of it, then fail so the connection is cut mid-stream."""
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream"), (b"cache-control", b"no-cache")],
        }
    )
    await send({"type": "http.response.body", "body": b"event: mess", "more_body": True})
    raise ConnectionResetError("Atlas injected a dropped connection")
