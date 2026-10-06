from __future__ import annotations

from collections.abc import Mapping

from ..domain.errors import CredentialResolutionError, EntityNotFoundError
from ..domain.models import ResolvedCredential
from ..persistence.sqlite_repository import SqliteControlPlaneRepository


class ConnectionCredentialResolver:
    """Resolves the downstream credential that belongs to exactly one connection.

    Static tokens are read from the environment at call time and never stored. OAuth
    connections return an empty token: the OAuth client supplies and refreshes the real one.
    """

    def __init__(
        self, repository: SqliteControlPlaneRepository, environ: Mapping[str, str]
    ) -> None:
        self._repository = repository
        self._environ = environ

    def resolve(
        self,
        *,
        local_identity_id: str,
        connection_id: str,
        server_id: str,
        required_scopes: frozenset[str],
    ) -> ResolvedCredential:
        try:
            connection = self._repository.get_connection(connection_id)
        except EntityNotFoundError:
            raise CredentialResolutionError("The connection no longer exists") from None
        if (
            connection.revoked_at is not None
            or connection.identity_id != local_identity_id
            or connection.server_id != server_id
        ):
            raise CredentialResolutionError("No credential is bound to this connection")

        reference = f"connection:{connection.connection_id}"
        base = {
            "credential_ref": reference,
            "local_identity_id": connection.identity_id,
            "connection_id": connection.connection_id,
            "server_id": connection.server_id,
        }
        if connection.credential_kind == "none":
            return ResolvedCredential(access_token="", scopes=required_scopes, **base)
        if connection.credential_kind == "env_token":
            token = self._environ.get(connection.credential_ref)
            if not token:
                raise CredentialResolutionError(
                    f"Environment variable {connection.credential_ref} is not set"
                )
            # A static token carries no discoverable scopes, so the owner's choice stands.
            return ResolvedCredential(access_token=token, scopes=required_scopes, **base)

        granted = frozenset(connection.scopes)
        missing = required_scopes - granted
        if missing:
            raise CredentialResolutionError(
                "The linked account lacks scopes: "
                + ", ".join(sorted(missing))
                + ". Run `login` for this connection to authorize them."
            )
        return ResolvedCredential(access_token="", scopes=granted, **base)
