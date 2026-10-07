from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from muse_mcp_adapter.credentials.resolver import InMemoryCredentialResolver
from muse_mcp_adapter.data_plane.approval import HmacApprovalVerifier
from muse_mcp_adapter.data_plane.tool_service import COMPLETED, ToolDataPlaneService
from muse_mcp_adapter.domain.errors import ApprovalRequiredError, UnknownOutcomeError
from muse_mcp_adapter.domain.models import ApprovalEvidence, DataPlaneRequest
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from tests.fixtures.active_runtime import active_repository
from tests.fixtures.tool_runtime import ConformanceToolClient

_NOW = "2026-10-04T18:05:00Z"
_APPROVALS = HmacApprovalVerifier(b"e2e-test-approval-secret-key-32-bytes!")


def test_phase_four_read_write_deduplication_and_unknown_recovery(
    tmp_path: Path,
) -> None:
    database = tmp_path / "adapter.db"
    with active_repository(database) as repository:
        credentials = InMemoryCredentialResolver()
        credentials.register(
            local_identity_id="muse-user-1",
            connection_id="project-connection-1",
            server_id="project-system",
            credential_ref="secret://project-system/muse-user-1",
            access_token="downstream-token",
            scopes=frozenset({"issues:read", "issues:write"}),
        )
        client = ConformanceToolClient()
        operation_ids: Iterator[str] = iter(
            ["operation-read", "operation-write", "operation-unknown"]
        )
        service = ToolDataPlaneService(
            repository,
            credentials,
            client,
            approval_verifier=_APPROVALS,
            clock=lambda: _NOW,
            operation_id_factory=lambda: next(operation_ids),
        )

        read = service.execute(_request("find_issue", {"id": "ISSUE-42"}))
        assert read.structured_content == {
            "id": "ISSUE-42",
            "title": "Example issue",
        }

        write_request = _request(
            "create_issue",
            {"title": "Customer issue"},
            idempotency_key="create-customer-issue",
        )
        with pytest.raises(ApprovalRequiredError) as approval:
            service.execute(write_request)
        approved = _request(
            "create_issue",
            {"title": "Customer issue"},
            idempotency_key="create-customer-issue",
            approval=_approval(approval.value),
        )
        created = service.execute(approved)
        duplicate = service.execute(approved)
        assert duplicate.replayed and not created.replayed
        assert replace(duplicate, replayed=False) == created
        assert created.operation_status == COMPLETED

        client.unknown_tools.add("create_issue")
        uncertain_request = _request(
            "create_issue",
            {"title": "Uncertain issue"},
            idempotency_key="create-uncertain-issue",
        )
        with pytest.raises(ApprovalRequiredError) as uncertain_approval:
            service.execute(uncertain_request)
        uncertain_approved = _request(
            "create_issue",
            {"title": "Uncertain issue"},
            idempotency_key="create-uncertain-issue",
            approval=_approval(uncertain_approval.value),
        )
        with pytest.raises(UnknownOutcomeError) as unknown:
            service.execute(uncertain_approved)
        with pytest.raises(UnknownOutcomeError):
            service.execute(uncertain_approved)

        service.reconcile_unknown(
            unknown.value.operation_id,
            actor="operator@example.test",
            outcome="not_applied",
        )
        client.unknown_tools.clear()
        recovered = service.execute(uncertain_approved)

        assert recovered.operation_status == COMPLETED
        assert len([call for call in client.calls if call.name == "create_issue"]) == 3
        assert "downstream-token" not in repr((read, created, recovered))

    calls_before_reopen = len(client.calls)
    with SqliteControlPlaneRepository(database) as reopened_repository:
        reopened_repository.initialize()
        reopened_service = ToolDataPlaneService(
            reopened_repository,
            credentials,
            client,
            approval_verifier=_APPROVALS,
            clock=lambda: _NOW,
            operation_id_factory=lambda: "must-not-be-created",
        )
        cached_after_restart = reopened_service.execute(approved)

    assert cached_after_restart.replayed
    assert replace(cached_after_restart, replayed=False) == created
    assert len(client.calls) == calls_before_reopen


def _request(
    operation: str,
    arguments: dict[str, object],
    *,
    idempotency_key: str | None = None,
    approval: ApprovalEvidence | None = None,
) -> DataPlaneRequest:
    return DataPlaneRequest(
        request_id="muse-request",
        server_id="project-system",
        public_operation_id=operation,
        local_identity_id="muse-user-1",
        connection_id="project-connection-1",
        arguments=arguments,
        idempotency_key=idempotency_key,
        approval=approval,
    )


def _approval(error: ApprovalRequiredError) -> ApprovalEvidence:
    return _APPROVALS.issue(
        approval_id=f"approval-{error.operation_id}",
        classification="write",
        action_digest=error.action_digest,
        approved_at="2026-10-04T18:00:00Z",
        expires_at="2026-10-04T18:10:00Z",
    )
