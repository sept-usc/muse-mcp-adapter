"""Regression tests: operations never strand in RUNNING and approvals/outcomes stay trustworthy."""

from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from threading import Event

import pytest

from muse_mcp_adapter.application.control_plane_service import ControlPlaneService
from muse_mcp_adapter.credentials.resolver import InMemoryCredentialResolver
from muse_mcp_adapter.data_plane.approval import HmacApprovalVerifier
from muse_mcp_adapter.data_plane.tool_client import (
    McpInputRequired,
    McpPreDispatchError,
)
from muse_mcp_adapter.data_plane.tool_service import (
    APPROVAL_REQUIRED,
    COMPLETED,
    FAILED,
    INPUT_REQUIRED,
    RUNNING,
    UNKNOWN,
    ToolDataPlaneService,
)
from muse_mcp_adapter.discovery.client import ManifestDiscoveryClient
from muse_mcp_adapter.domain.errors import (
    ApprovalRequiredError,
    DownstreamUnavailableError,
    InputRequiredError,
    RequestValidationError,
    ServerDisabledError,
    UnknownOutcomeError,
)
from muse_mcp_adapter.domain.models import (
    ApprovalEvidence,
    DataPlaneRequest,
    McpContentBlock,
    McpToolResult,
    ResolvedCredential,
)
from muse_mcp_adapter.operations.lifecycle import OperationLifecycleService
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from tests.fixtures.active_runtime import active_repository
from tests.fixtures.tool_runtime import ConformanceToolClient

_NOW = "2026-10-04T18:05:00Z"
_APPROVALS = HmacApprovalVerifier(b"integrity-test-approval-secret-32-bytes")
_FORGER = HmacApprovalVerifier(b"forger-forger-forger-forger-forger-forg")


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
    repository: SqliteControlPlaneRepository, client: ConformanceToolClient
) -> ToolDataPlaneService:
    ids: Iterator[str] = iter(f"operation-{index}" for index in range(100))
    return ToolDataPlaneService(
        repository,
        _resolver(),
        client,
        approval_verifier=_APPROVALS,
        clock=lambda: _NOW,
        operation_id_factory=lambda: next(ids),
    )


def _request(
    operation: str,
    arguments: Mapping[str, object],
    *,
    key: str | None = None,
    approval: ApprovalEvidence | None = None,
) -> DataPlaneRequest:
    return DataPlaneRequest(
        request_id="request-1",
        server_id="project-system",
        public_operation_id=operation,
        local_identity_id="user-1",
        connection_id="connection-1",
        arguments=arguments,
        idempotency_key=key,
        approval=approval,
    )


def _sign(
    verifier: HmacApprovalVerifier, error: ApprovalRequiredError, approval_id: str = "approval-1"
) -> ApprovalEvidence:
    return verifier.issue(
        approval_id=approval_id,
        classification="write",
        action_digest=error.action_digest,
        approved_at="2026-10-04T18:00:00Z",
        expires_at="2026-10-04T18:10:00Z",
    )


def _approved_write(
    service: ToolDataPlaneService, *, key: str = "key-1", title: str = "Example"
) -> ApprovalEvidence:
    with pytest.raises(ApprovalRequiredError) as raised:
        service.execute(_request("create_issue", {"title": title}, key=key))
    return _sign(_APPROVALS, raised.value)


class _Scripted(ConformanceToolClient):
    """Conformance client whose next call can be scripted to raise or return."""

    def __init__(self) -> None:
        super().__init__()
        self.script: list[Callable[[], McpToolResult]] = []
        self.answers: list[Mapping[str, object] | None] = []

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
        self.answers.append(elicitation_answer)
        self.progress_hook(progress_callback)
        if self.script:
            return self.script.pop(0)()
        return super().call_tool(
            endpoint=endpoint,
            name=name,
            arguments=arguments,
            credential=credential,
            idempotency_key=idempotency_key,
            timeout_seconds=timeout_seconds,
            progress_callback=progress_callback,
            elicitation_answer=elicitation_answer,
            cancellation=cancellation,
            log_callback=log_callback,
        )

    def progress_hook(
        self, callback: Callable[[float, float | None, str | None], None] | None
    ) -> None:
        del callback


def _example(approval: ApprovalEvidence) -> DataPlaneRequest:
    return _request("create_issue", {"title": "Example"}, key="key-1", approval=approval)


def _raise(error: BaseException) -> Callable[[], McpToolResult]:
    def raiser() -> McpToolResult:
        raise error

    return raiser


# --- Finding 1: forged / replayed approvals ------------------------------------------------


def test_forged_approval_is_rejected_and_the_write_never_runs(tmp_path: Path) -> None:
    with active_repository(tmp_path / "a.db") as repository:
        client = _Scripted()
        service = _service(repository, client)
        with pytest.raises(ApprovalRequiredError) as raised:
            service.execute(_request("create_issue", {"title": "x"}, key="k"))
        forged = _sign(_FORGER, raised.value)

        with pytest.raises(ApprovalRequiredError, match="signature"):
            service.execute(_request("create_issue", {"title": "x"}, key="k", approval=forged))

        assert client.calls == []
        assert service.get_operation(raised.value.operation_id).status == APPROVAL_REQUIRED


def test_an_approval_cannot_be_replayed_for_a_second_operation(tmp_path: Path) -> None:
    with active_repository(tmp_path / "a.db") as repository:
        client = _Scripted()
        service = _service(repository, client)
        first = _approved_write(service, key="k1", title="one")
        service.execute(_request("create_issue", {"title": "one"}, key="k1", approval=first))

        with pytest.raises(ApprovalRequiredError) as second:
            service.execute(_request("create_issue", {"title": "two"}, key="k2"))
        replay = _APPROVALS.issue(
            approval_id=first.approval_id,
            classification="write",
            action_digest=second.value.action_digest,
            approved_at="2026-10-04T18:00:00Z",
            expires_at="2026-10-04T18:10:00Z",
        )
        with pytest.raises(ApprovalRequiredError, match="already been used"):
            service.execute(_request("create_issue", {"title": "two"}, key="k2", approval=replay))


# --- Finding 2: published policy gaps and unexpected errors never strand RUNNING -----------


def test_policy_without_timeout_or_scopes_publishes_with_defaults_and_executes(
    tmp_path: Path,
) -> None:
    manifest = {
        "protocolVersion": "2026-07-28",
        "serverInfo": {"name": "s"},
        "capabilities": {},
        "primitives": [
            {
                "type": "tool",
                "name": "find_issue",
                "inputSchema": {"type": "object"},
                "annotations": {"readOnlyHint": True},
                "resultContentTypes": ["text/plain"],
            }
        ],
    }
    with SqliteControlPlaneRepository(tmp_path / "a.db") as repository:
        repository.initialize()
        control = ControlPlaneService(repository, clock=lambda: _NOW)
        control.register_server(
            server_id="project-system",
            display_name="P",
            endpoint="https://mcp.example.test/mcp",
            auth_profile="bearer_token",
        )
        control.discover("project-system", ManifestDiscoveryClient(manifest))
        control.publish_catalog(
            server_id="project-system",
            catalog_version="1.0.0",
            policies=[
                {
                    "primitiveKey": "tool:find_issue",
                    "allowed": True,
                    "publicOperationId": "find_issue",
                    "classification": "read",
                    "maxResultBytes": 1000,
                }
            ],
        )
        control.activate_catalog("project-system", "1.0.0")
        client = _Scripted()
        client.results["find_issue"] = McpToolResult(
            content=(McpContentBlock(block_type="text", text="ok"),)
        )
        response = _service(repository, client).execute(_request("find_issue", {}))
        assert response.operation_status == COMPLETED


def test_unexpected_error_after_write_dispatch_marks_unknown_not_running(tmp_path: Path) -> None:
    with active_repository(tmp_path / "a.db") as repository:
        client = _Scripted()
        client.script = [_raise(RuntimeError("boom"))]
        service = _service(repository, client)
        approval = _approved_write(service)

        with pytest.raises(RuntimeError, match="boom"):
            service.execute(_example(approval))

        record = service.get_operation("operation-0")
        assert record.status == UNKNOWN
        with pytest.raises(UnknownOutcomeError):
            service.execute(_example(approval))


def test_unexpected_error_in_a_read_fails_retryably(tmp_path: Path) -> None:
    with active_repository(tmp_path / "a.db") as repository:
        client = _Scripted()
        client.script = [_raise(RuntimeError("boom"))]
        service = _service(repository, client)

        with pytest.raises(RuntimeError):
            service.execute(_request("find_issue", {"id": "ISSUE-42"}))

        record = service.get_operation("operation-0")
        assert record.status == FAILED and record.retryable


# --- Finding 5: failures before dispatch are retryable, not UNKNOWN ------------------------


def test_pre_dispatch_failure_is_a_retryable_failure_and_the_write_can_be_retried(
    tmp_path: Path,
) -> None:
    with active_repository(tmp_path / "a.db") as repository:
        client = _Scripted()
        client.script = [_raise(McpPreDispatchError("connect failed"))]
        service = _service(repository, client)
        approval = _approved_write(service)
        request = _request("create_issue", {"title": "Example"}, key="key-1", approval=approval)

        with pytest.raises(DownstreamUnavailableError, match="connect failed"):
            service.execute(request)
        record = service.get_operation("operation-0")
        assert record.status == FAILED and record.retryable
        assert record.error_code == "PRE_DISPATCH_FAILURE"

        response = service.execute(request)  # same key and same approval: safe to retry
        assert response.operation_status == COMPLETED


# --- Finding 6: a rejected result after a write is not reported as a plain failure ---------


def test_rejected_write_result_is_unknown_and_requires_reconciliation(tmp_path: Path) -> None:
    with active_repository(tmp_path / "a.db") as repository:
        client = _Scripted()
        client.results["create_issue"] = McpToolResult(
            content=(), structured_content={"unexpected": True}
        )
        service = _service(repository, client)
        approval = _approved_write(service)

        with pytest.raises(UnknownOutcomeError, match="result was rejected"):
            service.execute(_example(approval))

        record = service.get_operation("operation-0")
        assert record.status == UNKNOWN
        assert record.error_code == "DOWNSTREAM_RESULT_REJECTED"


# --- Finding 7: input-required flow and cancellation ---------------------------------------


def test_input_required_then_resume_reruns_the_call_with_the_answer(tmp_path: Path) -> None:
    schema = {"type": "object", "properties": {"team": {"type": "string"}}, "required": ["team"]}
    with active_repository(tmp_path / "a.db") as repository:
        client = _Scripted()
        client.script = [_raise(McpInputRequired("Which team?", schema=schema))]
        service = _service(repository, client)
        approval = _approved_write(service)
        request = _request("create_issue", {"title": "Example"}, key="key-1", approval=approval)

        with pytest.raises(InputRequiredError) as raised:
            service.execute(request)
        operation_id = raised.value.operation_id
        assert service.get_operation(operation_id).status == INPUT_REQUIRED

        with pytest.raises(InputRequiredError):  # a plain retry must not re-dispatch
            service.execute(request)

        with pytest.raises(RequestValidationError):
            service.resume_with_input(request, operation_id=operation_id, inputs={"team": 3})

        response = service.resume_with_input(
            request, operation_id=operation_id, inputs={"team": "platform"}
        )
        assert response.operation_status == COMPLETED
        assert client.answers[-1] == {"team": "platform"}


def test_cancelling_a_running_write_only_records_a_request(tmp_path: Path) -> None:
    with active_repository(tmp_path / "a.db") as repository:
        client = _Scripted()
        service = _service(repository, client)
        approval = _approved_write(service)

        def cancel_mid_call() -> McpToolResult:
            lifecycle = OperationLifecycleService(repository, clock=lambda: _NOW)
            assert lifecycle.cancel("operation-0", reason="user").status == RUNNING
            return client.results["create_issue"]

        client.script = [cancel_mid_call]
        response = service.execute(
            _request("create_issue", {"title": "Example"}, key="key-1", approval=approval)
        )

        assert response.operation_status == COMPLETED  # the write really ran, so it is not hidden
        kinds = [event.event_type for event in repository.list_operation_events("operation-0")]
        assert "cancel_requested" in kinds


# --- Finding 11: bounded progress and trustworthy reconciliation ---------------------------


def test_progress_events_are_bounded(tmp_path: Path) -> None:
    class Chatty(_Scripted):
        def progress_hook(
            self, callback: Callable[[float, float | None, str | None], None] | None
        ) -> None:
            assert callback is not None
            for index in range(500):
                callback(float(index), None, "x" * 5000)

    with active_repository(tmp_path / "a.db") as repository:
        service = _service(repository, Chatty())
        service.execute(_request("find_issue", {"id": "ISSUE-42"}))

        events = [
            event
            for event in repository.list_operation_events("operation-0")
            if event.event_type == "progress"
        ]
        assert len(events) == 200
        assert all(len(str(event.data["message"])) <= 500 for event in events)


def test_reconciliation_requires_an_actor_and_revalidates_the_response(tmp_path: Path) -> None:
    with active_repository(tmp_path / "a.db") as repository:
        client = _Scripted()
        client.script = [_raise(RuntimeError("lost"))]
        service = _service(repository, client)
        approval = _approved_write(service)
        with pytest.raises(RuntimeError):
            service.execute(_example(approval))
        good = service.execute  # keep linters quiet about unused names

        from muse_mcp_adapter.domain.models import DataPlaneResponse

        def response(structured: Mapping[str, object]) -> DataPlaneResponse:
            return DataPlaneResponse(
                request_id="request-1",
                catalog_version="1.0.0",
                public_operation_id="create_issue",
                primitive_type="tool",
                provenance={},
                operation_id="operation-0",
                operation_status=COMPLETED,
                structured_content=structured,
            )

        with pytest.raises(RequestValidationError, match="actor"):
            service.reconcile_unknown("operation-0", actor="", outcome="not_applied")
        with pytest.raises(RequestValidationError, match="Tool output"):
            service.reconcile_unknown(
                "operation-0", actor="ops", outcome="completed", response=response({"bad": 1})
            )
        assert good is not None

        record = service.reconcile_unknown(
            "operation-0", actor="ops", outcome="completed", response=response({"id": "ISSUE-100"})
        )
        assert record.status == COMPLETED
        reconciled = [
            event
            for event in repository.list_operation_events("operation-0")
            if event.event_type == "reconciled"
        ]
        assert reconciled[0].data["actor"] == "ops"


# --- Disabled servers are blocked at runtime ----------------------------------------------


def test_disabled_server_cannot_execute(tmp_path: Path) -> None:
    with active_repository(tmp_path / "a.db") as repository:
        ControlPlaneService(repository, clock=lambda: _NOW).set_server_enabled(
            "project-system", enabled=False
        )
        with pytest.raises(ServerDisabledError):
            _service(repository, _Scripted()).execute(_request("find_issue", {"id": "ISSUE-42"}))
