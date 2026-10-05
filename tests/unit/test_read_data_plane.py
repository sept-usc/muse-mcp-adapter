from pathlib import Path

import pytest

from muse_mcp_adapter.credentials.resolver import InMemoryCredentialResolver
from muse_mcp_adapter.data_plane.read_service import ReadDataPlaneService
from muse_mcp_adapter.domain.errors import (
    DownstreamContentError,
    OperationNotFoundError,
    RequestValidationError,
    UnsupportedPrimitiveError,
)
from muse_mcp_adapter.domain.models import (
    DataPlaneRequest,
    McpPromptMessage,
    McpPromptResult,
    McpResourceContent,
    McpResourceResult,
)
from tests.fixtures.active_runtime import active_repository
from tests.fixtures.read_runtime import ConformanceReadClient


def _resolver() -> InMemoryCredentialResolver:
    resolver = InMemoryCredentialResolver()
    resolver.register(
        local_identity_id="user-1",
        connection_id="connection-1",
        server_id="project-system",
        credential_ref="secret://project-system/user-1",
        access_token="token",
        scopes=frozenset({"issues:read", "issues:write", "workspace:read"}),
    )
    return resolver


def _request(operation: str, arguments: dict[str, object]) -> DataPlaneRequest:
    return DataPlaneRequest(
        request_id="request-1",
        server_id="project-system",
        public_operation_id=operation,
        local_identity_id="user-1",
        connection_id="connection-1",
        arguments=arguments,
    )


def test_template_resource_arguments_are_encoded_and_constrained(tmp_path: Path) -> None:
    with active_repository(tmp_path / "adapter.db") as repository:
        client = ConformanceReadClient()
        service = ReadDataPlaneService(repository, _resolver(), client)

        response = service.execute(_request("read_issue_document", {"issueId": "ISSUE-42"}))

        assert response.contents[0].text == "Issue 42 diagnostic document"
        assert client.resource_calls[0].uri == "mcp://issues/ISSUE-42/document"
        with pytest.raises(RequestValidationError, match="unsafe value"):
            service.execute(_request("read_issue_document", {"issueId": ".."}))
        with pytest.raises(RequestValidationError, match="Unexpected"):
            service.execute(
                _request(
                    "read_issue_document",
                    {"issueId": "ISSUE-42", "server": "other"},
                )
            )


def test_resource_mime_type_and_returned_uri_are_enforced(tmp_path: Path) -> None:
    with active_repository(tmp_path / "adapter.db") as repository:
        client = ConformanceReadClient()
        service = ReadDataPlaneService(repository, _resolver(), client)
        client.resources["mcp://workspace/guide"] = McpResourceResult(
            contents=(
                McpResourceContent(
                    uri="mcp://other/resource",
                    mime_type="application/octet-stream",
                    text="unsafe",
                ),
            )
        )

        with pytest.raises(DownstreamContentError, match="URI"):
            service.execute(_request("read_workspace_guide", {}))

        client.resources["mcp://workspace/guide"] = McpResourceResult(
            contents=(
                McpResourceContent(
                    uri="mcp://workspace/guide",
                    mime_type="application/octet-stream",
                    text="unsafe",
                ),
            )
        )
        with pytest.raises(DownstreamContentError, match="MIME type"):
            service.execute(_request("read_workspace_guide", {}))

        client.resources["mcp://workspace/guide"] = McpResourceResult(
            contents=(
                McpResourceContent(
                    uri="mcp://workspace/guide",
                    mime_type="text/plain",
                    text="x" * 262145,
                ),
            )
        )
        with pytest.raises(DownstreamContentError, match="size limit"):
            service.execute(_request("read_workspace_guide", {}))


def test_prompt_arguments_roles_and_provenance_are_preserved(tmp_path: Path) -> None:
    with active_repository(tmp_path / "adapter.db") as repository:
        client = ConformanceReadClient()
        service = ReadDataPlaneService(repository, _resolver(), client)

        response = service.execute(_request("get_triage_prompt", {"issueId": "ISSUE-42"}))

        assert response.messages[0].role == "user"
        assert response.messages[0].content[1].uri == "mcp://issues/ISSUE-42/document"
        assert response.provenance["mcpName"] == "triage_issue"
        assert client.prompt_calls[0].arguments == {"issueId": "ISSUE-42"}

        with pytest.raises(RequestValidationError, match="Missing prompt arguments"):
            service.execute(_request("get_triage_prompt", {}))

        client.prompts["triage_issue"] = McpPromptResult(
            description=None,
            messages=(McpPromptMessage(role="system", content=()),),
        )
        with pytest.raises(DownstreamContentError, match="disallowed role"):
            service.execute(_request("get_triage_prompt", {"issueId": "ISSUE-42"}))


def test_tool_operations_are_rejected_until_phase_four(tmp_path: Path) -> None:
    with active_repository(tmp_path / "adapter.db") as repository:
        service = ReadDataPlaneService(repository, _resolver(), ConformanceReadClient())

        with pytest.raises(UnsupportedPrimitiveError, match="not available"):
            service.execute(_request("find_issue", {"id": "ISSUE-42"}))

        with pytest.raises(OperationNotFoundError, match="not published"):
            service.execute(_request("arbitrary_operation", {}))
