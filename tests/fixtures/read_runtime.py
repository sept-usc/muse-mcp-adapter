from collections.abc import Mapping
from dataclasses import dataclass

from muse_mcp_adapter.data_plane.mcp_client import McpReadClient
from muse_mcp_adapter.data_plane.read_service import ReadDataPlaneService
from muse_mcp_adapter.domain.errors import DownstreamContentError
from muse_mcp_adapter.domain.models import (
    DataPlaneRequest,
    DataPlaneResponse,
    McpContentBlock,
    McpPromptMessage,
    McpPromptResult,
    McpResourceContent,
    McpResourceResult,
    ResolvedCredential,
)


@dataclass(frozen=True, slots=True)
class RecordedResourceCall:
    endpoint: str
    uri: str
    credential_ref: str


@dataclass(frozen=True, slots=True)
class RecordedPromptCall:
    endpoint: str
    name: str
    arguments: Mapping[str, str]
    credential_ref: str


class ConformanceReadClient(McpReadClient):
    def __init__(self) -> None:
        self.resource_calls: list[RecordedResourceCall] = []
        self.prompt_calls: list[RecordedPromptCall] = []
        self.resources: dict[str, McpResourceResult] = {
            "mcp://workspace/guide": McpResourceResult(
                contents=(
                    McpResourceContent(
                        uri="mcp://workspace/guide",
                        mime_type="text/markdown",
                        text="# Workspace guide\nUse project conventions.",
                        annotations={"audience": ["user", "assistant"]},
                    ),
                )
            ),
            "mcp://issues/ISSUE-42/document": McpResourceResult(
                contents=(
                    McpResourceContent(
                        uri="mcp://issues/ISSUE-42/document",
                        mime_type="text/plain",
                        text="Issue 42 diagnostic document",
                    ),
                )
            ),
        }
        self.prompts: dict[str, McpPromptResult] = {
            "triage_issue": McpPromptResult(
                description="Triage an issue",
                messages=(
                    McpPromptMessage(
                        role="user",
                        content=(
                            McpContentBlock(
                                block_type="text",
                                text="Triage the selected issue and explain the likely cause.",
                            ),
                            McpContentBlock(
                                block_type="resource",
                                resource=McpResourceContent(
                                    uri="mcp://issues/ISSUE-42/document",
                                    mime_type="text/plain",
                                    text="Issue 42 diagnostic document",
                                ),
                            ),
                        ),
                    ),
                ),
            )
        }

    def read_resource(
        self,
        *,
        endpoint: str,
        uri: str,
        credential: ResolvedCredential,
    ) -> McpResourceResult:
        self.resource_calls.append(
            RecordedResourceCall(
                endpoint=endpoint,
                uri=uri,
                credential_ref=credential.credential_ref,
            )
        )
        result = self.resources.get(uri)
        if result is None:
            raise DownstreamContentError(f"Fixture resource {uri} was not found")
        return result

    def get_prompt(
        self,
        *,
        endpoint: str,
        name: str,
        arguments: Mapping[str, str],
        credential: ResolvedCredential,
    ) -> McpPromptResult:
        self.prompt_calls.append(
            RecordedPromptCall(
                endpoint=endpoint,
                name=name,
                arguments=dict(arguments),
                credential_ref=credential.credential_ref,
            )
        )
        result = self.prompts.get(name)
        if result is None:
            raise DownstreamContentError(f"Fixture prompt {name} was not found")
        return result


class FakeMuseClient:
    def __init__(
        self,
        service: ReadDataPlaneService,
        *,
        server_id: str,
        local_identity_id: str,
        connection_id: str,
    ) -> None:
        self._service = service
        self._server_id = server_id
        self._local_identity_id = local_identity_id
        self._connection_id = connection_id
        self._request_number = 0

    def execute(
        self, public_operation_id: str, arguments: Mapping[str, object]
    ) -> DataPlaneResponse:
        self._request_number += 1
        return self._service.execute(
            DataPlaneRequest(
                request_id=f"muse-request-{self._request_number}",
                server_id=self._server_id,
                public_operation_id=public_operation_id,
                local_identity_id=self._local_identity_id,
                connection_id=self._connection_id,
                arguments=arguments,
            )
        )
