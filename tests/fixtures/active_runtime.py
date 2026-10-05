from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from muse_mcp_adapter.application.control_plane_service import ControlPlaneService
from muse_mcp_adapter.discovery.client import ManifestDiscoveryClient
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from tests.fixtures.phase_one import discovery_manifest, phase_one_fixture


@contextmanager
def active_repository(database: Path) -> Generator[SqliteControlPlaneRepository]:
    times = iter(
        [
            "2026-10-04T18:00:00Z",
            "2026-10-04T18:05:00Z",
            "2026-10-04T18:06:00Z",
        ]
    )
    with SqliteControlPlaneRepository(database) as repository:
        repository.initialize()
        control = ControlPlaneService(repository, clock=lambda: next(times))
        control.register_server(
            server_id="project-system",
            display_name="Project System",
            endpoint="https://mcp.example.test/mcp",
            auth_profile="delegated_oauth",
        )
        control.discover(
            "project-system",
            ManifestDiscoveryClient(discovery_manifest(), discovered_at="2026-10-04T18:01:00Z"),
        )
        control.publish_catalog(
            server_id="project-system",
            catalog_version="1.0.0",
            policies=phase_one_fixture()["policies"],
        )
        control.activate_catalog("project-system", "1.0.0")
        yield repository
