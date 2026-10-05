import pytest

from muse_mcp_adapter.discovery.client import ManifestDiscoveryClient
from muse_mcp_adapter.domain.errors import InvalidStateError
from muse_mcp_adapter.domain.models import ServerRegistration
from tests.fixtures.phase_one import discovery_manifest


def _server() -> ServerRegistration:
    return ServerRegistration(
        server_id="project-system",
        display_name="Project System",
        endpoint="https://mcp.example.test/mcp",
        auth_profile="delegated_oauth",
        provider_pack=None,
        enabled=True,
        created_at="2026-10-04T18:00:00Z",
        updated_at="2026-10-04T18:00:00Z",
    )


def test_manifest_discovery_is_deterministic_and_immutable() -> None:
    client = ManifestDiscoveryClient(discovery_manifest(), discovered_at="2026-10-04T18:01:00Z")

    first = client.discover(_server())
    second = client.discover(_server())

    assert first.snapshot_digest == second.snapshot_digest
    assert len(first.primitives) == 5
    with pytest.raises(TypeError):
        first.primitives[0]["name"] = "changed"  # type: ignore[index]


def test_manifest_discovery_rejects_missing_protocol_version() -> None:
    manifest = discovery_manifest()
    del manifest["protocolVersion"]

    with pytest.raises(InvalidStateError, match="protocolVersion"):
        ManifestDiscoveryClient(manifest).discover(_server())
