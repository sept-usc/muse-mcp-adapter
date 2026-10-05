import pytest

from muse_mcp_adapter.credentials.resolver import InMemoryCredentialResolver
from muse_mcp_adapter.domain.errors import CredentialResolutionError


def test_credentials_are_isolated_by_identity_connection_and_server() -> None:
    resolver = InMemoryCredentialResolver()
    resolver.register(
        local_identity_id="user-1",
        connection_id="connection-1",
        server_id="project-system",
        credential_ref="secret://project-system/user-1",
        access_token="never-log-this-token",
        scopes=frozenset({"issues:read"}),
    )

    credential = resolver.resolve(
        local_identity_id="user-1",
        connection_id="connection-1",
        server_id="project-system",
        required_scopes=frozenset({"issues:read"}),
    )

    assert credential.credential_ref == "secret://project-system/user-1"
    assert "never-log-this-token" not in repr(credential)
    with pytest.raises(CredentialResolutionError):
        resolver.resolve(
            local_identity_id="user-2",
            connection_id="connection-1",
            server_id="project-system",
            required_scopes=frozenset({"issues:read"}),
        )


def test_credential_must_cover_published_scopes() -> None:
    resolver = InMemoryCredentialResolver()
    resolver.register(
        local_identity_id="user-1",
        connection_id="connection-1",
        server_id="project-system",
        credential_ref="secret://project-system/user-1",
        access_token="token",
        scopes=frozenset({"issues:read"}),
    )

    with pytest.raises(CredentialResolutionError, match="issues:write"):
        resolver.resolve(
            local_identity_id="user-1",
            connection_id="connection-1",
            server_id="project-system",
            required_scopes=frozenset({"issues:write"}),
        )
