"""Lifecycle, artifact, audit, provider-pack and concurrency regressions from the review."""

import stat
import threading
from pathlib import Path

import pytest

from muse_mcp_adapter.application.control_plane_service import ControlPlaneService
from muse_mcp_adapter.discovery.client import ManifestDiscoveryClient
from muse_mcp_adapter.domain.errors import (
    EntityNotFoundError,
    InvalidStateError,
    RequestValidationError,
)
from muse_mcp_adapter.domain.models import OperationRecord
from muse_mcp_adapter.operations.artifacts import FileArtifactStore
from muse_mcp_adapter.operations.cancellation import CancellationRegistry
from muse_mcp_adapter.operations.lifecycle import OperationLifecycleService
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from muse_mcp_adapter.providers import (
    ProviderConformanceRunner,
    build_endpoint,
    load_provider_pack,
)
from tests.fixtures.active_runtime import active_repository
from tests.fixtures.phase_one import discovery_manifest, phase_one_fixture

_NOW = "2026-10-04T19:00:00Z"


def _operation(operation_id: str, classification: str, status: str = "RUNNING") -> OperationRecord:
    return OperationRecord(
        operation_id=operation_id,
        request_id=f"request-{operation_id}",
        server_id="project-system",
        catalog_version="1.0.0",
        public_operation_id="create_issue",
        local_identity_id="user-1",
        connection_id="connection-1",
        classification=classification,
        status=status,
        idempotency_key=f"key-{operation_id}",
        request_digest="sha256:abc",
        approval_id=None,
        created_at="2026-10-04T18:00:00Z",
        updated_at="2026-10-04T18:00:00Z",
        result=None,
        error_code=None,
        error_message=None,
        retryable=False,
    )


# --- Finding 7: lifecycle -------------------------------------------------------------------


def test_resume_validates_input_against_the_schema_that_was_requested(tmp_path: Path) -> None:
    with active_repository(tmp_path / "a.db") as repository:
        repository.create_operation(_operation("op", "write"))
        lifecycle = OperationLifecycleService(repository, clock=lambda: _NOW)
        lifecycle.request_input(
            "op",
            schema={
                "type": "object",
                "properties": {"team": {"type": "string"}},
                "required": ["team"],
            },
            message="Which team?",
        )
        with pytest.raises(RequestValidationError):
            lifecycle.resume("op", inputs={"team": 7})
        assert repository.get_operation("op").status == "INPUT_REQUIRED"
        assert lifecycle.resume("op", inputs={"team": "platform"}).status == "RUNNING"
        with pytest.raises(InvalidStateError):  # no longer waiting for input
            lifecycle.resume("op", inputs={"team": "platform"})


def test_cancel_semantics_differ_for_unstarted_work_running_reads_and_running_writes(
    tmp_path: Path,
) -> None:
    registry = CancellationRegistry()
    with active_repository(tmp_path / "a.db") as repository:
        for name, classification, status in (
            ("pending", "write", "PENDING"),
            ("read", "read", "RUNNING"),
            ("write", "write", "RUNNING"),
        ):
            repository.create_operation(_operation(name, classification, status))
        lifecycle = OperationLifecycleService(
            repository, clock=lambda: _NOW, cancellation_registry=registry
        )

        assert lifecycle.cancel("pending", reason="no").status == "CANCELLED"

        with registry.track("read") as signal:
            assert lifecycle.cancel("read", reason="stop").status == "RUNNING"
            assert signal.is_set()  # the executing client is told to stop

        with registry.track("write") as signal:
            assert lifecycle.cancel("write", reason="stop").status == "RUNNING"
            assert not signal.is_set()  # a write is never aborted mid-flight


def test_stale_running_operations_recover_to_safe_states(tmp_path: Path) -> None:
    with active_repository(tmp_path / "a.db") as repository:
        repository.create_operation(_operation("read-op", "read"))
        repository.create_operation(_operation("write-op", "write"))
        recovered = OperationLifecycleService(repository, clock=lambda: _NOW).recover_stale(
            updated_before="2026-10-04T18:30:00Z"
        )
        statuses = {item.operation_id: (item.status, item.retryable) for item in recovered}
        assert statuses == {"read-op": ("FAILED", True), "write-op": ("UNKNOWN", False)}


# --- Low: artifacts are owner-only and owned by an operation ---------------------------------


def test_artifacts_are_private_and_bound_to_their_operation(tmp_path: Path) -> None:
    with active_repository(tmp_path / "a.db") as repository:
        repository.create_operation(_operation("op-a", "write"))
        repository.create_operation(_operation("op-b", "write"))
        store = FileArtifactStore(
            tmp_path / "artifacts",
            repository,
            clock=lambda: _NOW,
            artifact_id_factory=lambda: "artifact-1",
        )
        record = store.put(
            operation_id="op-a",
            file_name="../../etc/passwd",
            media_type="text/plain",
            content=b"data",
        )
        assert record.file_name == "etc_passwd" or "/" not in record.file_name

        stored = tmp_path / "artifacts" / record.storage_path
        assert stat.S_IMODE(stored.stat().st_mode) == 0o600
        assert stat.S_IMODE(stored.parent.stat().st_mode) == 0o700
        assert store.read("artifact-1", operation_id="op-a") == b"data"
        with pytest.raises(EntityNotFoundError):
            store.read("artifact-1", operation_id="op-b")


# --- Design §18: administrative audit --------------------------------------------------------


def test_administrative_actions_are_audited_with_the_actor(tmp_path: Path) -> None:
    fixture = phase_one_fixture()
    times = iter(f"2026-10-04T18:0{index}:00Z" for index in range(9))
    with SqliteControlPlaneRepository(tmp_path / "a.db") as repository:
        repository.initialize()
        control = ControlPlaneService(repository, clock=lambda: next(times))
        control.register_server(
            server_id="project-system",
            display_name="P",
            endpoint="https://mcp.example.test/mcp",
            auth_profile="bearer_token",
            actor="alice",
        )
        control.discover(
            "project-system", ManifestDiscoveryClient(discovery_manifest()), actor="alice"
        )
        control.publish_catalog(
            server_id="project-system",
            catalog_version="1.0.0",
            policies=fixture["policies"],
            actor="bob",
        )
        control.activate_catalog("project-system", "1.0.0", actor="bob")
        control.set_server_enabled("project-system", enabled=False, actor="carol")

        trail = [(row["action"], row["actor"]) for row in repository.list_admin_audit()]
        assert trail == [
            ("server.register", "alice"),
            ("server.discover", "alice"),
            ("catalog.publish", "bob"),
            ("catalog.activate", "bob"),
            ("server.disable", "carol"),
        ]


# --- Finding 9: provider packs carry enforced safety ----------------------------------------


def test_supabase_read_only_default_cannot_be_dropped_without_an_explicit_override() -> None:
    pack = load_provider_pack("supabase")
    with pytest.raises(RequestValidationError, match="read-only"):
        build_endpoint(pack, options={"project_ref": "p", "read_only": "false"})
    unsafe = build_endpoint(
        pack, options={"project_ref": "p", "read_only": "false"}, allow_unsafe_overrides=True
    )
    assert "read_only=false" in unsafe


def test_registration_enforces_the_provider_pack(tmp_path: Path) -> None:
    with SqliteControlPlaneRepository(tmp_path / "a.db") as repository:
        repository.initialize()
        control = ControlPlaneService(repository, clock=lambda: _NOW)

        def register(**overrides: str) -> None:
            fields = {
                "server_id": "supabase-prod",
                "display_name": "Supabase",
                "endpoint": "https://mcp.supabase.com/mcp?read_only=true&project_ref=abc",
                "auth_profile": "oauth_pkce",
                "provider_pack": "supabase",
            }
            fields.update(overrides)
            control.register_server(**fields)

        with pytest.raises(InvalidStateError, match="does not match provider pack"):
            register(endpoint="https://evil.example.test/mcp")
        with pytest.raises(InvalidStateError, match="Missing provider options"):
            register(endpoint="https://mcp.supabase.com/mcp?read_only=true")
        with pytest.raises(InvalidStateError, match="read-only"):
            register(endpoint="https://mcp.supabase.com/mcp?read_only=false&project_ref=abc")
        with pytest.raises(InvalidStateError, match="not supported"):
            register(auth_profile="static_password")
        with pytest.raises(InvalidStateError, match="HTTPS"):
            register(endpoint="http://mcp.supabase.com/mcp?read_only=true&project_ref=abc")

        register()
        assert [item.server_id for item in control.list_servers()] == ["supabase-prod"]


def test_provider_conformance_flags_missing_primitives_and_unsupported_auth() -> None:
    from muse_mcp_adapter.domain.models import ServerRegistration

    server = ServerRegistration(
        "provider-conformance", "P", "https://mcp.example.test/mcp", "x", None, True, _NOW, _NOW
    )
    manifest = discovery_manifest()
    manifest["primitives"] = [p for p in manifest["primitives"] if p["type"] != "tool"]
    snapshot = ManifestDiscoveryClient(manifest, discovered_at=_NOW).discover(server)

    result = ProviderConformanceRunner().evaluate(
        load_provider_pack("linear"), snapshot, auth_mode="password"
    )
    assert not result.passed
    assert any("Missing primitive types" in issue for issue in result.issues)
    assert any("authentication mode" in issue for issue in result.issues)


# --- Finding: one repository instance is safe to share across worker threads -----------------


def test_repository_is_safe_under_concurrent_threads(tmp_path: Path) -> None:
    with active_repository(tmp_path / "a.db") as repository:
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                for item in range(25):
                    repository.create_operation(_operation(f"op-{index}-{item}", "read", "PENDING"))
                    repository.transition_operation(
                        f"op-{index}-{item}",
                        expected_statuses=("PENDING",),
                        status="RUNNING",
                        updated_at=_NOW,
                    )
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert repository.get_operation("op-7-24").status == "RUNNING"
