from pathlib import Path

import pytest

from muse_mcp_adapter.control_plane.compile_catalog import compile_catalog
from muse_mcp_adapter.discovery.client import ManifestDiscoveryClient
from muse_mcp_adapter.domain.errors import ConflictError, InvalidStateError
from muse_mcp_adapter.domain.models import ServerRegistration
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from tests.fixtures.phase_one import discovery_manifest, phase_one_fixture

_NOW = "2026-10-04T18:00:00Z"


def _server() -> ServerRegistration:
    return ServerRegistration(
        server_id="project-system",
        display_name="Project System",
        endpoint="https://mcp.example.test/mcp",
        auth_profile="delegated_oauth",
        provider_pack="project-system",
        enabled=True,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_repository_persists_registration_and_discovery(tmp_path: Path) -> None:
    database = tmp_path / "adapter.db"
    server = _server()
    snapshot = ManifestDiscoveryClient(
        discovery_manifest(), discovered_at="2026-10-04T18:01:00Z"
    ).discover(server)

    with SqliteControlPlaneRepository(database) as repository:
        repository.initialize()
        repository.create_server(server)
        saved = repository.save_discovery(snapshot)

    with SqliteControlPlaneRepository(database) as repository:
        repository.initialize()
        assert repository.get_server(server.server_id) == server
        loaded = repository.get_latest_discovery(server.server_id)
        assert loaded.snapshot_id == saved.snapshot_id
        assert loaded.snapshot_digest == snapshot.snapshot_digest
        assert loaded.primitives == snapshot.primitives


def test_repository_rejects_duplicate_server(tmp_path: Path) -> None:
    with SqliteControlPlaneRepository(tmp_path / "adapter.db") as repository:
        repository.initialize()
        repository.create_server(_server())
        with pytest.raises(ConflictError, match="already exists"):
            repository.create_server(_server())


def test_catalog_activation_and_rollback_are_durable(tmp_path: Path) -> None:
    database = tmp_path / "adapter.db"
    fixture = phase_one_fixture()
    first = compile_catalog(**fixture)
    second_fixture = phase_one_fixture()
    second_fixture["catalog_version"] = "2.0.0"
    second_fixture["published_at"] = "2026-10-04T19:00:00.000Z"
    second = compile_catalog(**second_fixture)

    with SqliteControlPlaneRepository(database) as repository:
        repository.initialize()
        repository.create_server(_server())
        repository.save_catalog(first)
        repository.save_catalog(second)
        repository.activate_catalog("project-system", "1.0.0", "2026-10-04T19:01:00Z")
        repository.activate_catalog("project-system", "2.0.0", "2026-10-04T19:02:00Z")
        rollback = repository.rollback_catalog("project-system", "2026-10-04T19:03:00Z")
        assert rollback.catalog_version == "1.0.0"
        assert repository.get_active_catalog("project-system").catalog_version == "1.0.0"
        with pytest.raises(InvalidStateError, match="no rollback target"):
            repository.rollback_catalog("project-system", "2026-10-04T19:04:00Z")

    with SqliteControlPlaneRepository(database) as repository:
        repository.initialize()
        assert repository.get_active_catalog("project-system").catalog_version == "1.0.0"
