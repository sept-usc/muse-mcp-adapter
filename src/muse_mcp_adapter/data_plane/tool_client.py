from collections.abc import Callable, Mapping
from threading import Event
from typing import Protocol

from ..domain.models import McpToolResult, ResolvedCredential


class McpToolClient(Protocol):
    def call_tool(
        self,
        *,
        endpoint: str,
        name: str,
        arguments: Mapping[str, object],
        credential: ResolvedCredential,
        idempotency_key: str | None,
        timeout_seconds: int | None = None,
        progress_callback: Callable[[float, float | None, str | None], None] | None = None,
        elicitation_answer: Mapping[str, object] | None = None,
        cancellation: Event | None = None,
    ) -> McpToolResult: ...


class McpToolExecutionFailure(RuntimeError):
    """The downstream server definitively rejected or failed the call."""

    def __init__(self, message: str, *, code: str = "TOOL_EXECUTION_FAILED") -> None:
        self.code = code
        super().__init__(message)


class McpPreDispatchError(RuntimeError):
    """The request provably never reached the downstream tool; retrying is safe."""


class McpReauthorizationRequired(McpPreDispatchError):
    """The linked account's tokens are missing or cannot be refreshed; the owner must log in."""


class McpUnknownOutcome(RuntimeError):
    """The request may have reached the downstream server, but its result is unknown."""


class McpInputRequired(RuntimeError):
    """The downstream tool asked the user for input before it could continue."""

    def __init__(self, message: str, *, schema: Mapping[str, object]) -> None:
        self.schema = schema
        super().__init__(message)


class McpCancelled(RuntimeError):
    """The adapter cancelled a read call and closed its MCP request stream."""
