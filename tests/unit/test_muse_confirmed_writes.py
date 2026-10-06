"""Default write authorization: Muse confirms with its user, the adapter keeps the safety net."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from muse_mcp_adapter.credentials.resolver import InMemoryCredentialResolver
from muse_mcp_adapter.data_plane.tool_service import ToolDataPlaneService
from muse_mcp_adapter.domain.errors import RequestValidationError, UnknownOutcomeError
from muse_mcp_adapter.domain.models import ApprovalEvidence, DataPlaneRequest
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from tests.fixtures.active_runtime import active_repository
from tests.fixtures.tool_runtime import ConformanceToolClient

_NOW = "2026-10-04T18:05:00Z"


def _service(
    repository: SqliteControlPlaneRepository, client: ConformanceToolClient
) -> ToolDataPlaneService:
    resolver = InMemoryCredentialResolver()
    resolver.register(
        local_identity_id="user-1",
        connection_id="connection-1",
        server_id="project-system",
        credential_ref="secret://project-system/user-1",
        access_token="token",
        scopes=frozenset({"issues:read", "issues:write"}),
    )
    ids: Iterator[str] = iter(f"operation-{index}" for index in range(50))
    return ToolDataPlaneService(  # no approval_verifier: Muse's confirmation is the control
        repository,
        resolver,
        client,
        clock=lambda: _NOW,
        operation_id_factory=lambda: next(ids),
    )


def _write(key: str | None = "key-1", approval: ApprovalEvidence | None = None) -> DataPlaneRequest:
    return DataPlaneRequest(
        request_id="request-7",
        server_id="project-system",
        public_operation_id="create_issue",
        local_identity_id="user-1",
        connection_id="connection-1",
        arguments={"title": "Example"},
        idempotency_key=key,
        approval=approval,
    )


def test_a_write_runs_without_signed_evidence(tmp_path: Path) -> None:
    with active_repository(tmp_path / "a.db") as repository:
        client = ConformanceToolClient()
        response = _service(repository, client).execute(_write())

        assert response.operation_status == "COMPLETED"
        assert len(client.calls) == 1
        record = repository.get_operation(str(response.operation_id))
        assert record.approval_id == "muse-confirmed:request-7"


def test_writes_still_need_an_idempotency_key(tmp_path: Path) -> None:
    with active_repository(tmp_path / "a.db") as repository:
        client = ConformanceToolClient()
        with pytest.raises(RequestValidationError, match="idempotency"):
            _service(repository, client).execute(_write(key=None))
        assert client.calls == []


def test_a_repeated_write_runs_once_and_returns_the_stored_result(tmp_path: Path) -> None:
    with active_repository(tmp_path / "a.db") as repository:
        client = ConformanceToolClient()
        service = _service(repository, client)
        first = service.execute(_write())
        second = service.execute(_write())

        assert second.operation_id == first.operation_id
        assert len(client.calls) == 1


def test_an_uncertain_write_is_still_never_blindly_retried(tmp_path: Path) -> None:
    with active_repository(tmp_path / "a.db") as repository:
        client = ConformanceToolClient()
        client.unknown_tools.add("create_issue")
        service = _service(repository, client)

        with pytest.raises(UnknownOutcomeError):
            service.execute(_write())
        with pytest.raises(UnknownOutcomeError):
            service.execute(_write())
        assert len(client.calls) == 1


def test_supplied_approval_evidence_is_ignored_in_this_mode(tmp_path: Path) -> None:
    junk = ApprovalEvidence(
        "a", "write", "sha256:x", "2020-01-01T00:00:00Z", "2020-01-01T00:00:01Z"
    )
    with active_repository(tmp_path / "a.db") as repository:
        client = ConformanceToolClient()
        response = _service(repository, client).execute(_write(approval=junk))
        assert response.operation_status == "COMPLETED"
