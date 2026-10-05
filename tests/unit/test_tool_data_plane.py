from collections.abc import Iterator
from pathlib import Path

import pytest

from muse_mcp_adapter.credentials.resolver import InMemoryCredentialResolver
from muse_mcp_adapter.data_plane.approval import HmacApprovalVerifier
from muse_mcp_adapter.data_plane.tool_service import (
    APPROVAL_REQUIRED,
    COMPLETED,
    FAILED,
    UNKNOWN,
    ToolDataPlaneService,
)
from muse_mcp_adapter.domain.errors import (
    ApprovalRequiredError,
    DownstreamContentError,
    DownstreamToolError,
    IdempotencyConflictError,
    RequestValidationError,
    UnknownOutcomeError,
)
from muse_mcp_adapter.domain.models import (
    ApprovalEvidence,
    DataPlaneRequest,
    McpToolResult,
)
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from tests.fixtures.active_runtime import active_repository
from tests.fixtures.tool_runtime import ConformanceToolClient

_NOW = "2026-10-04T18:05:00Z"
_APPROVALS = HmacApprovalVerifier(b"unit-test-approval-secret-key-32-bytes")


def _resolver() -> InMemoryCredentialResolver:
    resolver = InMemoryCredentialResolver()
    resolver.register(
        local_identity_id="user-1",
        connection_id="connection-1",
        server_id="project-system",
        credential_ref="secret://project-system/user-1",
        access_token="token",
        scopes=frozenset({"issues:read", "issues:write"}),
    )
    return resolver


def _service(
    repository: SqliteControlPlaneRepository,
    client: ConformanceToolClient,
    operation_ids: Iterator[str],
) -> ToolDataPlaneService:
    return ToolDataPlaneService(
        repository,
        _resolver(),
        client,
        approval_verifier=_APPROVALS,
        clock=lambda: _NOW,
        operation_id_factory=lambda: next(operation_ids),
    )


def _request(
    operation: str,
    arguments: dict[str, object],
    *,
    request_id: str = "request-1",
    idempotency_key: str | None = None,
    approval: ApprovalEvidence | None = None,
) -> DataPlaneRequest:
    return DataPlaneRequest(
        request_id=request_id,
        server_id="project-system",
        public_operation_id=operation,
        local_identity_id="user-1",
        connection_id="connection-1",
        arguments=arguments,
        idempotency_key=idempotency_key,
        approval=approval,
    )


def _approval(error: ApprovalRequiredError) -> ApprovalEvidence:
    return _APPROVALS.issue(
        approval_id="approval-1",
        classification="write",
        action_digest=error.action_digest,
        approved_at="2026-10-04T18:00:00Z",
        expires_at="2026-10-04T18:10:00Z",
    )


def test_tool_input_and_output_schemas_are_enforced(tmp_path: Path) -> None:
    with active_repository(tmp_path / "adapter.db") as repository:
        client = ConformanceToolClient()
        service = _service(repository, client, iter(["operation-input", "operation-output"]))

        with pytest.raises(RequestValidationError, match="required property"):
            service.execute(_request("find_issue", {}))
        assert client.calls == []

        client.results["find_issue"] = McpToolResult(
            content=(), structured_content={"unexpected": True}
        )
        with pytest.raises(DownstreamContentError, match="Tool output"):
            service.execute(_request("find_issue", {"id": "ISSUE-42"}))
        assert service.get_operation("operation-input").status == FAILED


def test_write_requires_exact_unexpired_approval(tmp_path: Path) -> None:
    with active_repository(tmp_path / "adapter.db") as repository:
        service = _service(repository, ConformanceToolClient(), iter(["operation-1"]))
        request = _request("create_issue", {"title": "Example"}, idempotency_key="create-1")

        with pytest.raises(ApprovalRequiredError) as missing:
            service.execute(request)
        assert missing.value.operation_id == "operation-1"
        assert service.get_operation("operation-1").status == APPROVAL_REQUIRED

        invalid = _APPROVALS.issue(
            approval_id="approval-expired",
            classification="write",
            action_digest=missing.value.action_digest,
            approved_at="2026-10-04T17:00:00Z",
            expires_at="2026-10-04T17:01:00Z",
        )
        with pytest.raises(ApprovalRequiredError, match="not currently valid"):
            service.execute(
                _request(
                    "create_issue",
                    {"title": "Example"},
                    idempotency_key="create-1",
                    approval=invalid,
                )
            )


def test_idempotent_write_executes_once_and_returns_cached_result(tmp_path: Path) -> None:
    with active_repository(tmp_path / "adapter.db") as repository:
        client = ConformanceToolClient()
        service = _service(repository, client, iter(["operation-1"]))
        initial = _request("create_issue", {"title": "Example"}, idempotency_key="create-1")
        with pytest.raises(ApprovalRequiredError) as required:
            service.execute(initial)
        evidence = _approval(required.value)

        first = service.execute(
            _request(
                "create_issue",
                {"title": "Example"},
                request_id="request-2",
                idempotency_key="create-1",
                approval=evidence,
            )
        )
        cached = service.execute(
            _request(
                "create_issue",
                {"title": "Example"},
                request_id="request-3",
                idempotency_key="create-1",
                approval=evidence,
            )
        )

        assert first == cached
        assert first.operation_status == COMPLETED
        assert len(client.calls) == 1
        assert service.get_operation("operation-1").status == COMPLETED

        with pytest.raises(IdempotencyConflictError):
            service.execute(
                _request(
                    "create_issue",
                    {"title": "Different"},
                    idempotency_key="create-1",
                    approval=evidence,
                )
            )


def test_unknown_write_is_not_retried_until_reconciled(tmp_path: Path) -> None:
    with active_repository(tmp_path / "adapter.db") as repository:
        client = ConformanceToolClient()
        client.unknown_tools.add("create_issue")
        service = _service(repository, client, iter(["operation-unknown"]))
        initial = _request("create_issue", {"title": "Example"}, idempotency_key="create-unknown")
        with pytest.raises(ApprovalRequiredError) as required:
            service.execute(initial)
        approved = _request(
            "create_issue",
            {"title": "Example"},
            idempotency_key="create-unknown",
            approval=_approval(required.value),
        )

        with pytest.raises(UnknownOutcomeError) as unknown:
            service.execute(approved)
        assert service.get_operation(unknown.value.operation_id).status == UNKNOWN

        with pytest.raises(UnknownOutcomeError):
            service.execute(approved)
        assert len(client.calls) == 1

        reconciled = service.reconcile_unknown(
            "operation-unknown", actor="operator@example.test", outcome="not_applied"
        )
        assert reconciled.status == FAILED
        assert reconciled.retryable is True

        client.unknown_tools.clear()
        response = service.execute(approved)
        assert response.operation_status == COMPLETED
        assert len(client.calls) == 2


def test_confirmed_downstream_failure_is_terminal_and_not_retried(tmp_path: Path) -> None:
    with active_repository(tmp_path / "adapter.db") as repository:
        client = ConformanceToolClient()
        client.failed_tools.add("create_issue")
        service = _service(repository, client, iter(["operation-failed"]))
        request = _request("create_issue", {"title": "Example"}, idempotency_key="create-failed")
        with pytest.raises(ApprovalRequiredError) as required:
            service.execute(request)
        approved = _request(
            "create_issue",
            {"title": "Example"},
            idempotency_key="create-failed",
            approval=_approval(required.value),
        )

        with pytest.raises(DownstreamToolError, match="rejected"):
            service.execute(approved)
        operation = service.get_operation("operation-failed")
        assert operation.status == FAILED
        assert operation.retryable is False

        with pytest.raises(DownstreamToolError, match="rejected"):
            service.execute(approved)
        assert len(client.calls) == 1
