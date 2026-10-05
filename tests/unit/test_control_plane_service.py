from copy import deepcopy
from pathlib import Path

import pytest

from muse_mcp_adapter.application.control_plane_service import ControlPlaneService
from muse_mcp_adapter.discovery.client import ManifestDiscoveryClient
from muse_mcp_adapter.domain.errors import InvalidStateError
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from tests.fixtures.phase_one import discovery_manifest, phase_one_fixture


def test_service_runs_registration_discovery_and_publication(tmp_path: Path) -> None:
    times = iter(
        [
            "2026-10-04T18:00:00Z",
            "2026-10-04T18:05:00Z",
            "2026-10-04T18:06:00Z",
        ]
    )
    with SqliteControlPlaneRepository(tmp_path / "adapter.db") as repository:
        repository.initialize()
        service = ControlPlaneService(repository, clock=lambda: next(times))
        service.register_server(
            server_id="project-system",
            display_name="Project System",
            endpoint="https://mcp.example.test/mcp",
            auth_profile="delegated_oauth",
        )
        snapshot = service.discover(
            "project-system",
            ManifestDiscoveryClient(discovery_manifest(), discovered_at="2026-10-04T18:01:00Z"),
        )
        fixture = phase_one_fixture()
        catalog = service.publish_catalog(
            server_id="project-system",
            catalog_version="1.0.0",
            policies=fixture["policies"],
        )
        activation = service.activate_catalog("project-system", "1.0.0")

        assert snapshot.snapshot_id is not None
        assert len(catalog.primitives) == 5
        assert activation.catalog_version == "1.0.0"
        assert service.get_active_catalog("project-system") == catalog

        changed_manifest = deepcopy(discovery_manifest())
        changed_tool = next(
            item for item in changed_manifest["primitives"] if item["name"] == "find_issue"
        )
        changed_tool["requiredScopes"].append("teams:read")
        service.discover(
            "project-system",
            ManifestDiscoveryClient(changed_manifest, discovered_at="2026-10-04T18:07:00Z"),
        )
        assert any(
            event.drift_type == "authorization" for event in service.get_drift("project-system")
        )


def test_service_rejects_invalid_registration(tmp_path: Path) -> None:
    with SqliteControlPlaneRepository(tmp_path / "adapter.db") as repository:
        repository.initialize()
        service = ControlPlaneService(repository)
        with pytest.raises(InvalidStateError, match="server_id"):
            service.register_server(
                server_id="INVALID ID",
                display_name="Project System",
                endpoint="https://mcp.example.test/mcp",
                auth_profile="delegated_oauth",
            )
