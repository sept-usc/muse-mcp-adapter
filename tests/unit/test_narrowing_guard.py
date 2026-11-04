"""An actor that may only make access smaller cannot put a wider catalog live, whoever asks.

Chat setup lets the agent narrow a connection on its own. The rule that it may never widen one is
kept where a catalog goes live, so it holds even for a caller that forgot to check.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from muse_mcp_adapter.application.control_plane_service import ControlPlaneService
from muse_mcp_adapter.discovery.client import ManifestDiscoveryClient
from muse_mcp_adapter.domain.errors import AuthorizationError, EntityNotFoundError
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from tests.fixtures.phase_one import discovery_manifest, phase_one_fixture

AGENT = "chat-setup:agent"
SERVER = "project-system"


def _policies(*switched_off: str) -> list[dict[str, Any]]:
    policies = deepcopy(phase_one_fixture()["policies"])
    for policy in policies:
        if policy["primitiveKey"] in switched_off:
            policy["allowed"] = False
    return policies


def _service(repository: SqliteControlPlaneRepository) -> ControlPlaneService:
    service = ControlPlaneService(repository, narrowing_only_actors=(AGENT,))
    service.register_server(
        server_id=SERVER,
        display_name="Project System",
        endpoint="https://mcp.example.test/mcp",
        auth_profile="delegated_oauth",
    )
    service.discover(
        SERVER, ManifestDiscoveryClient(discovery_manifest(), discovered_at="2026-10-04T18:01:00Z")
    )
    service.publish_catalog(server_id=SERVER, catalog_version="1", policies=_policies())
    service.activate_catalog(SERVER, "1")
    return service


def test_a_narrowing_actor_may_put_a_smaller_catalog_live(tmp_path: Path) -> None:
    with SqliteControlPlaneRepository(tmp_path / "adapter.db") as repository:
        repository.initialize()
        service = _service(repository)
        service.publish_catalog(
            server_id=SERVER, catalog_version="2", policies=_policies("tool:create_issue")
        )

        service.activate_catalog(SERVER, "2", actor=AGENT)

        live = service.get_active_catalog(SERVER)
        assert live.catalog_version == "2"
        assert "create_issue" not in {item.public_operation_id for item in live.primitives}


def test_a_narrowing_actor_cannot_put_a_wider_catalog_live(tmp_path: Path) -> None:
    with SqliteControlPlaneRepository(tmp_path / "adapter.db") as repository:
        repository.initialize()
        service = _service(repository)
        service.publish_catalog(
            server_id=SERVER, catalog_version="2", policies=_policies("tool:create_issue")
        )
        service.activate_catalog(SERVER, "2", actor=AGENT)
        service.publish_catalog(server_id=SERVER, catalog_version="3", policies=_policies())

        with pytest.raises(AuthorizationError, match="only make access smaller"):
            service.activate_catalog(SERVER, "3", actor=AGENT)

        assert service.get_active_catalog(SERVER).catalog_version == "2"


def test_a_narrowing_actor_cannot_roll_back_to_something_wider(tmp_path: Path) -> None:
    with SqliteControlPlaneRepository(tmp_path / "adapter.db") as repository:
        repository.initialize()
        service = _service(repository)
        service.publish_catalog(
            server_id=SERVER, catalog_version="2", policies=_policies("tool:create_issue")
        )
        service.activate_catalog(SERVER, "2", actor=AGENT)

        with pytest.raises(AuthorizationError, match="rollback"):
            service.rollback_catalog(SERVER, actor=AGENT)

        assert service.get_active_catalog(SERVER).catalog_version == "2"


def test_everyone_else_may_still_widen(tmp_path: Path) -> None:
    with SqliteControlPlaneRepository(tmp_path / "adapter.db") as repository:
        repository.initialize()
        service = _service(repository)
        service.publish_catalog(
            server_id=SERVER, catalog_version="2", policies=_policies("tool:create_issue")
        )
        service.activate_catalog(SERVER, "2", actor=AGENT)
        service.publish_catalog(server_id=SERVER, catalog_version="3", policies=_policies())

        service.activate_catalog(SERVER, "3", actor="chat-setup:owner")

        assert service.get_active_catalog(SERVER).catalog_version == "3"


def test_a_narrowing_actor_cannot_put_the_first_catalog_for_a_server_live(tmp_path: Path) -> None:
    with SqliteControlPlaneRepository(tmp_path / "adapter.db") as repository:
        repository.initialize()
        service = ControlPlaneService(repository, narrowing_only_actors=(AGENT,))
        service.register_server(
            server_id=SERVER,
            display_name="Project System",
            endpoint="https://mcp.example.test/mcp",
            auth_profile="delegated_oauth",
        )
        service.discover(
            SERVER,
            ManifestDiscoveryClient(discovery_manifest(), discovered_at="2026-10-04T18:01:00Z"),
        )
        service.publish_catalog(server_id=SERVER, catalog_version="1", policies=_policies())

        with pytest.raises(AuthorizationError, match="only make access smaller"):
            service.activate_catalog(SERVER, "1", actor=AGENT)

        with pytest.raises(EntityNotFoundError):
            service.get_active_catalog(SERVER)  # nothing went live


def test_a_narrowing_actor_may_put_live_a_catalog_that_exposes_nothing(tmp_path: Path) -> None:
    with SqliteControlPlaneRepository(tmp_path / "adapter.db") as repository:
        repository.initialize()
        service = ControlPlaneService(repository, narrowing_only_actors=(AGENT,))
        service.register_server(
            server_id=SERVER,
            display_name="Project System",
            endpoint="https://mcp.example.test/mcp",
            auth_profile="delegated_oauth",
        )
        service.discover(
            SERVER,
            ManifestDiscoveryClient(discovery_manifest(), discovered_at="2026-10-04T18:01:00Z"),
        )
        everything = [item["primitiveKey"] for item in phase_one_fixture()["policies"]]
        service.publish_catalog(
            server_id=SERVER, catalog_version="1", policies=_policies(*everything)
        )

        service.activate_catalog(SERVER, "1", actor=AGENT)

        assert service.get_active_catalog(SERVER).primitives == ()
