from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Event

from muse_mcp_adapter.data_plane.tool_client import (
    McpCancelled,
    McpToolClient,
    McpToolExecutionFailure,
    McpUnknownOutcome,
)
from muse_mcp_adapter.domain.models import (
    McpContentBlock,
    McpToolResult,
    ResolvedCredential,
)


@dataclass(frozen=True, slots=True)
class RecordedToolCall:
    name: str
    arguments: Mapping[str, object]
    credential_ref: str
    idempotency_key: str | None


class ConformanceToolClient(McpToolClient):
    def __init__(self) -> None:
        self.calls: list[RecordedToolCall] = []
        self.unknown_tools: set[str] = set()
        self.failed_tools: set[str] = set()
        self.results: dict[str, McpToolResult] = {
            "find_issue": McpToolResult(
                content=(McpContentBlock(block_type="text", text="Issue ISSUE-42"),),
                structured_content={"id": "ISSUE-42", "title": "Example issue"},
            ),
            "create_issue": McpToolResult(
                content=(McpContentBlock(block_type="text", text="Created ISSUE-100"),),
                structured_content={"id": "ISSUE-100"},
            ),
        }

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
        log_callback: Callable[[str, str], None] | None = None,
    ) -> McpToolResult:
        del endpoint
        del timeout_seconds
        del elicitation_answer, log_callback
        self.calls.append(
            RecordedToolCall(
                name=name,
                arguments=dict(arguments),
                credential_ref=credential.credential_ref,
                idempotency_key=idempotency_key,
            )
        )
        if progress_callback is not None:
            progress_callback(1.0, 1.0, "Fixture tool completed")
        if cancellation is not None and cancellation.is_set():
            raise McpCancelled("Fixture tool was cancelled")
        if name in self.unknown_tools:
            raise McpUnknownOutcome("Connection closed after the request was sent")
        if name in self.failed_tools:
            raise McpToolExecutionFailure("Downstream tool rejected the request")
        return self.results[name]
