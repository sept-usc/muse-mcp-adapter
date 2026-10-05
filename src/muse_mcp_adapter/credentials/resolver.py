from typing import Protocol

from ..domain.errors import CredentialResolutionError
from ..domain.models import ResolvedCredential


class CredentialResolver(Protocol):
    def resolve(
        self,
        *,
        local_identity_id: str,
        connection_id: str,
        server_id: str,
        required_scopes: frozenset[str],
    ) -> ResolvedCredential: ...


class InMemoryCredentialResolver:
    """Local and test resolver; production secret stores implement the same protocol."""

    def __init__(self) -> None:
        self._credentials: dict[tuple[str, str, str], ResolvedCredential] = {}
        self._reference_owner: dict[str, tuple[str, str, str]] = {}

    def register(
        self,
        *,
        local_identity_id: str,
        connection_id: str,
        server_id: str,
        credential_ref: str,
        access_token: str,
        scopes: frozenset[str],
    ) -> None:
        key = (local_identity_id, connection_id, server_id)
        owner = self._reference_owner.setdefault(credential_ref, key)
        if owner != key:
            raise ValueError(
                f"Credential reference {credential_ref} is already bound to another "
                "identity, connection, or server"
            )
        self._credentials[key] = ResolvedCredential(
            credential_ref=credential_ref,
            access_token=access_token,
            scopes=scopes,
            local_identity_id=local_identity_id,
            connection_id=connection_id,
            server_id=server_id,
        )

    def resolve(
        self,
        *,
        local_identity_id: str,
        connection_id: str,
        server_id: str,
        required_scopes: frozenset[str],
    ) -> ResolvedCredential:
        key = (local_identity_id, connection_id, server_id)
        credential = self._credentials.get(key)
        if credential is None:
            raise CredentialResolutionError(
                "No credential binding exists for the identity, connection, and server"
            )
        missing = required_scopes - credential.scopes
        if missing:
            raise CredentialResolutionError(
                f"Credential {credential.credential_ref} is missing required scopes: "
                + ", ".join(sorted(missing))
            )
        return credential
