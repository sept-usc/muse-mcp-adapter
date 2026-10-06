"""Per-connection tokens, slugs and downstream credentials."""

from pathlib import Path

import pytest

from muse_mcp_adapter.application.control_plane_service import ControlPlaneService
from muse_mcp_adapter.connections import ConnectionCredentialResolver, ConnectionService
from muse_mcp_adapter.domain.errors import (
    AuthenticationError,
    CredentialResolutionError,
    EntityNotFoundError,
    InvalidStateError,
)
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from muse_mcp_adapter.transport.security import EndpointSecurityPolicy

_NOW = "2026-10-05T10:00:00Z"


def _setup(tmp_path: Path) -> tuple[SqliteControlPlaneRepository, ConnectionService]:
    repository = SqliteControlPlaneRepository(tmp_path / "a.db")
    repository.initialize()
    ControlPlaneService(
        repository, clock=lambda: _NOW, endpoint_policy=EndpointSecurityPolicy.local_development()
    ).register_server(
        server_id="simple",
        display_name="Simple",
        endpoint="http://127.0.0.1:9101/mcp",
        auth_profile="none",
    )
    return repository, ConnectionService(repository, clock=lambda: _NOW)


def test_link_returns_the_token_once_and_stores_only_its_hash(tmp_path: Path) -> None:
    repository, connections = _setup(tmp_path)
    linked = connections.link(server_id="simple", label="My Muse", credential_kind="none")

    assert linked.token.startswith("mcpa_") and len(linked.token) > 40
    assert linked.token not in repr(linked.record)
    assert connections.authenticate(linked.token).connection_id == linked.record.connection_id
    stored = repository.get_connection(linked.record.connection_id)
    assert stored.token_hash != linked.token and len(stored.token_hash) == 64
    assert len(linked.record.slug) >= 22  # unguessable public spec path


def test_distinct_links_get_distinct_tokens_and_slugs(tmp_path: Path) -> None:
    _, connections = _setup(tmp_path)
    first = connections.link(server_id="simple", label="a", credential_kind="none")
    second = connections.link(server_id="simple", label="b", credential_kind="none")
    assert first.token != second.token
    assert first.record.slug != second.record.slug


@pytest.mark.parametrize("token", [None, "", "mcpa_wrong", "not-a-token"])
def test_unknown_tokens_are_rejected(tmp_path: Path, token: str | None) -> None:
    _, connections = _setup(tmp_path)
    connections.link(server_id="simple", label="a", credential_kind="none")
    with pytest.raises(AuthenticationError):
        connections.authenticate(token)


def test_revoked_and_rotated_tokens_stop_working(tmp_path: Path) -> None:
    _, connections = _setup(tmp_path)
    linked = connections.link(server_id="simple", label="a", credential_kind="none")

    rotated = connections.rotate_token(linked.record.connection_id)
    with pytest.raises(AuthenticationError):
        connections.authenticate(linked.token)
    assert connections.authenticate(rotated.token).slug == linked.record.slug

    connections.unlink(linked.record.connection_id)
    with pytest.raises(AuthenticationError, match="revoked"):
        connections.authenticate(rotated.token)
    with pytest.raises(EntityNotFoundError):
        connections.by_slug(linked.record.slug)
    with pytest.raises(InvalidStateError, match="already revoked"):
        connections.unlink(linked.record.connection_id)


def test_link_validates_its_inputs(tmp_path: Path) -> None:
    _, connections = _setup(tmp_path)
    with pytest.raises(EntityNotFoundError):
        connections.link(server_id="missing", label="a", credential_kind="none")
    with pytest.raises(InvalidStateError, match="credential_kind"):
        connections.link(server_id="simple", label="a", credential_kind="password")
    with pytest.raises(InvalidStateError, match="environment variable"):
        connections.link(server_id="simple", label="a", credential_kind="env_token")
    with pytest.raises(InvalidStateError, match="environment variable"):
        connections.link(
            server_id="simple", label="a", credential_kind="env_token", credential_ref="a b;rm"
        )
    with pytest.raises(InvalidStateError, match="label"):
        connections.link(server_id="simple", label=" ", credential_kind="none")


def test_actions_are_audited(tmp_path: Path) -> None:
    repository, connections = _setup(tmp_path)
    linked = connections.link(
        server_id="simple", label="a", credential_kind="none", actor="kishore"
    )
    connections.unlink(linked.record.connection_id, actor="kishore")
    trail = [(row["action"], row["actor"]) for row in repository.list_admin_audit("simple")]
    assert ("connection.link", "kishore") in trail
    assert ("connection.unlink", "kishore") in trail
    assert linked.token not in str(repository.list_admin_audit())


def test_resolver_returns_the_connections_own_credential(tmp_path: Path) -> None:
    repository, connections = _setup(tmp_path)
    static = connections.link(
        server_id="simple",
        label="static",
        credential_kind="env_token",
        credential_ref="PROJECT_TOKEN",
    )
    open_ = connections.link(server_id="simple", label="open", credential_kind="none")
    resolver = ConnectionCredentialResolver(repository, {"PROJECT_TOKEN": "downstream-secret"})

    def resolve(connection_id: str, scopes: frozenset[str] = frozenset()):  # type: ignore[no-untyped-def]
        return resolver.resolve(
            local_identity_id="owner",
            connection_id=connection_id,
            server_id="simple",
            required_scopes=scopes,
        )

    assert resolve(static.record.connection_id).access_token == "downstream-secret"
    assert resolve(open_.record.connection_id).access_token == ""
    assert "downstream-secret" not in repr(resolve(static.record.connection_id))

    with pytest.raises(CredentialResolutionError):  # wrong identity cannot borrow it
        resolver.resolve(
            local_identity_id="someone-else",
            connection_id=static.record.connection_id,
            server_id="simple",
            required_scopes=frozenset(),
        )
    connections.unlink(static.record.connection_id)
    with pytest.raises(CredentialResolutionError):
        resolve(static.record.connection_id)


def test_missing_environment_variable_is_a_clear_error(tmp_path: Path) -> None:
    repository, connections = _setup(tmp_path)
    linked = connections.link(
        server_id="simple", label="s", credential_kind="env_token", credential_ref="ABSENT_VAR"
    )
    with pytest.raises(CredentialResolutionError, match="ABSENT_VAR"):
        ConnectionCredentialResolver(repository, {}).resolve(
            local_identity_id="owner",
            connection_id=linked.record.connection_id,
            server_id="simple",
            required_scopes=frozenset(),
        )


def test_oauth_connections_must_hold_the_required_scopes(tmp_path: Path) -> None:
    repository, connections = _setup(tmp_path)
    linked = connections.link(
        server_id="simple", label="o", credential_kind="oauth", scopes=["issues:read"]
    )
    resolver = ConnectionCredentialResolver(repository, {})

    def resolve(scopes: frozenset[str]):  # type: ignore[no-untyped-def]
        return resolver.resolve(
            local_identity_id="owner",
            connection_id=linked.record.connection_id,
            server_id="simple",
            required_scopes=scopes,
        )

    assert resolve(frozenset({"issues:read"})).scopes == {"issues:read"}
    with pytest.raises(CredentialResolutionError, match="issues:write"):
        resolve(frozenset({"issues:write"}))
