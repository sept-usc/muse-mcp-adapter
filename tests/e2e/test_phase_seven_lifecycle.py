from pathlib import Path

from muse_mcp_adapter.domain.models import OperationRecord
from muse_mcp_adapter.operations.artifacts import FileArtifactStore
from muse_mcp_adapter.operations.audit import AuditRecorder
from muse_mcp_adapter.operations.lifecycle import OperationLifecycleService
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from tests.fixtures.active_runtime import active_repository


def test_durable_input_resume_artifact_audit_and_restart_recovery(tmp_path: Path) -> None:
    database = tmp_path / "adapter.db"
    artifacts = tmp_path / "artifacts"
    with active_repository(database) as repository:
        repository.create_operation(_operation("interactive", "write"))
        lifecycle = OperationLifecycleService(repository, clock=lambda: "2026-10-04T19:10:00Z")
        requested = lifecycle.request_input(
            "interactive",
            schema={"type": "object", "required": ["confirmation"]},
            message="Confirm the target project",
        )
        assert requested.status == "INPUT_REQUIRED"
        assert (
            lifecycle.resume("interactive", inputs={"confirmation": "project-a"}).status
            == "RUNNING"
        )

        store = FileArtifactStore(
            artifacts,
            repository,
            clock=lambda: "2026-10-04T19:11:00Z",
            artifact_id_factory=lambda: "artifact-1",
        )
        saved = store.put(
            operation_id="interactive",
            file_name="../report.json",
            media_type="application/json",
            content=b'{"status":"ready"}',
        )
        assert saved.file_name == "report.json"
        assert store.read("artifact-1", operation_id="interactive") == b'{"status":"ready"}'

        AuditRecorder(repository).record(
            "interactive",
            event_type="downstream_request",
            occurred_at="2026-10-04T19:12:00Z",
            data={"authorization": "Bearer secret", "request": {"title": "safe"}},
        )
        events = repository.list_operation_events("interactive")
        audit = next(event for event in events if event.event_type == "downstream_request")
        assert audit.data["authorization"] == "[REDACTED]"

        repository.create_operation(_operation("stale-read", "read"))
        repository.create_operation(_operation("stale-write", "write"))

    with SqliteControlPlaneRepository(database) as repository:
        repository.initialize()
        lifecycle = OperationLifecycleService(repository, clock=lambda: "2026-10-04T20:00:00Z")
        recovered = lifecycle.recover_stale(updated_before="2026-10-04T19:00:00Z")
        assert {(item.operation_id, item.status, item.retryable) for item in recovered} == {
            ("stale-read", "FAILED", True),
            ("stale-write", "UNKNOWN", False),
        }
        assert repository.get_artifact("artifact-1").content_digest.startswith("sha256:")


def _operation(operation_id: str, classification: str) -> OperationRecord:
    return OperationRecord(
        operation_id=operation_id,
        request_id=f"request-{operation_id}",
        server_id="project-system",
        catalog_version="1.0.0",
        public_operation_id="create_issue" if classification == "write" else "find_issue",
        local_identity_id="muse-user-1",
        connection_id="connection-1",
        classification=classification,
        status="RUNNING",
        idempotency_key=None,
        request_digest=f"digest-{operation_id}",
        approval_id=None,
        created_at="2026-10-04T18:00:00Z",
        updated_at="2026-10-04T18:00:00Z",
        result=None,
        error_code=None,
        error_message=None,
        retryable=False,
    )
