from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from ..domain.errors import AuthenticationError, EntityNotFoundError, InvalidStateError
from ..domain.models import ConnectionRecord
from ..persistence.sqlite_repository import SqliteControlPlaneRepository

TOKEN_PREFIX = "mcpa_"
CREDENTIAL_KINDS = ("none", "env_token", "oauth")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class LinkedConnection:
    """A connection plus its bearer token, which exists in plaintext only at link time."""

    record: ConnectionRecord
    token: str


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ConnectionService:
    """Creates, authenticates, rotates and revokes the links Muse uses to reach a server.

    Muse stores the bearer token. The adapter stores only its hash, so a leaked database does
    not reveal usable tokens. The token identifies the owner, the connection and the server, so
    callers never send any of those in a request.
    """

    def __init__(
        self,
        repository: SqliteControlPlaneRepository,
        *,
        clock: Callable[[], str] | None = None,
        token_factory: Callable[[], str] | None = None,
        slug_factory: Callable[[], str] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or _utc_now
        self._token_factory = token_factory or (lambda: TOKEN_PREFIX + secrets.token_urlsafe(32))
        self._slug_factory = slug_factory or (lambda: secrets.token_urlsafe(16))
        self._id_factory = id_factory or (lambda: "conn_" + secrets.token_hex(6))

    def link(
        self,
        *,
        server_id: str,
        label: str,
        credential_kind: str,
        credential_ref: str = "",
        scopes: Sequence[str] = (),
        identity_id: str = "owner",
        actor: str = "local-administrator",
    ) -> LinkedConnection:
        server = self._repository.get_server(server_id)
        if not server.enabled:
            raise InvalidStateError(f"Server {server_id} is disabled")
        if credential_kind not in CREDENTIAL_KINDS:
            raise InvalidStateError(
                "credential_kind must be one of: " + ", ".join(CREDENTIAL_KINDS)
            )
        if credential_kind == "env_token" and not _ENV_NAME.fullmatch(credential_ref):
            raise InvalidStateError(
                "env_token connections need the name of an environment variable"
            )
        if credential_kind != "env_token" and credential_ref:
            raise InvalidStateError("credential_ref is only used with env_token connections")
        if not label.strip():
            raise InvalidStateError("label is required")

        token = self._token_factory()
        record = self._repository.create_connection(
            ConnectionRecord(
                connection_id=self._id_factory(),
                slug=self._slug_factory(),
                token_hash=hash_token(token),
                identity_id=identity_id,
                server_id=server_id,
                label=label.strip(),
                credential_kind=credential_kind,
                credential_ref=credential_ref,
                scopes=tuple(sorted(set(scopes))),
                created_at=self._clock(),
                revoked_at=None,
            )
        )
        self._audit(actor, "connection.link", record)
        return LinkedConnection(record, token)

    def authenticate(self, token: str | None) -> ConnectionRecord:
        """Resolve a presented bearer token to its active connection."""
        if not token:
            raise AuthenticationError("A bearer token is required")
        try:
            record = self._repository.find_connection_by_token_hash(hash_token(token))
        except EntityNotFoundError:
            raise AuthenticationError("Bearer token is invalid") from None
        if record.revoked_at is not None:
            raise AuthenticationError("This connection has been revoked")
        return record

    def by_slug(self, slug: str) -> ConnectionRecord:
        try:
            record = self._repository.find_connection_by_slug(slug)
        except EntityNotFoundError:
            raise EntityNotFoundError("Connector was not found") from None
        if record.revoked_at is not None:
            raise EntityNotFoundError("Connector was not found")
        return record

    def unlink(self, connection_id: str, *, actor: str = "local-administrator") -> ConnectionRecord:
        record = self._repository.revoke_connection(connection_id, self._clock())
        self._audit(actor, "connection.unlink", record)
        return record

    def rotate_token(
        self, connection_id: str, *, actor: str = "local-administrator"
    ) -> LinkedConnection:
        token = self._token_factory()
        record = self._repository.replace_connection_token(connection_id, hash_token(token))
        self._audit(actor, "connection.rotate_token", record)
        return LinkedConnection(record, token)

    def list(self, server_id: str | None = None) -> tuple[ConnectionRecord, ...]:
        return self._repository.list_connections(server_id)

    def _audit(self, actor: str, action: str, record: ConnectionRecord) -> None:
        self._repository.record_admin_audit(
            occurred_at=self._clock(),
            actor=actor,
            action=action,
            server_id=record.server_id,
            data={"connectionId": record.connection_id, "label": record.label},
        )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
