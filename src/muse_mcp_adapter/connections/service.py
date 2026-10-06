from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..domain.errors import AuthenticationError, EntityNotFoundError, InvalidStateError
from ..domain.models import ConnectionRecord
from ..persistence.oauth_store import OAuthServerStore
from ..persistence.sqlite_repository import SqliteControlPlaneRepository

TOKEN_PREFIX = "mcpa_"
ACCESS_TOKEN_PREFIX = "mcpa_at_"
LINK_CODE_TTL_SECONDS = 24 * 3600
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to avoid misreading
CREDENTIAL_KINDS = ("none", "env_token", "oauth")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class LinkedConnection:
    """A connection plus its bearer token, which exists in plaintext only at link time."""

    record: ConnectionRecord
    token: str
    link_code: str | None = None
    link_code_expires_at: str | None = None


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
        link_code_factory: Callable[[], str] | None = None,
    ) -> None:
        self._repository = repository
        self._oauth = OAuthServerStore(repository)
        self._link_code_factory = link_code_factory or _new_link_code
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
        code, expires = self.issue_link_code(record.connection_id, actor=actor)
        return LinkedConnection(record, token, code, expires)

    def issue_link_code(
        self,
        connection_id: str,
        *,
        ttl_seconds: int = LINK_CODE_TTL_SECONDS,
        actor: str = "local-administrator",
    ) -> tuple[str, str]:
        """Create the code the owner types when Muse opens its sign-in page.

        It proves the person at that page is the owner, so a stranger who learns the connector
        URL cannot link their own Muse to it. It is shown once and expires.
        """
        code = self._link_code_factory()
        salt = secrets.token_hex(16)
        expires = (_parse(self._clock()) + timedelta(seconds=ttl_seconds)).isoformat()
        expires = expires.replace("+00:00", "Z")
        record = self._repository.set_connection_link_code(
            connection_id, _hash_code(code, salt), salt, expires
        )
        self._audit(actor, "connection.link_code", record)
        return code, expires

    def verify_link_code(self, connection_id: str, presented: str) -> bool:
        record = self._repository.get_connection(connection_id)
        if (
            record.revoked_at is not None
            or record.link_code_hash is None
            or record.link_code_salt is None
            or record.link_code_expires_at is None
            or record.link_code_expires_at <= self._clock()
        ):
            return False
        return hmac.compare_digest(
            _hash_code(presented, record.link_code_salt), record.link_code_hash
        )

    def authenticate(self, token: str | None) -> ConnectionRecord:
        """Resolve a presented bearer token to its active connection."""
        if not token:
            raise AuthenticationError("A bearer token is required")
        if token.startswith(ACCESS_TOKEN_PREFIX):
            return self._authenticate_access_token(token)
        try:
            record = self._repository.find_connection_by_token_hash(hash_token(token))
        except EntityNotFoundError:
            raise AuthenticationError("Bearer token is invalid") from None
        if record.revoked_at is not None:
            raise AuthenticationError("This connection has been revoked")
        return record

    def _authenticate_access_token(self, token: str) -> ConnectionRecord:
        """Accept an access token that this adapter issued to Muse through OAuth."""
        stored = self._oauth.get_token(hash_token(token))
        if (
            stored is None
            or stored.kind != "access"
            or stored.revoked_at is not None
            or stored.expires_at <= self._clock()
        ):
            raise AuthenticationError("Bearer token is invalid")
        try:
            record = self._repository.get_connection(stored.connection_id)
        except EntityNotFoundError:
            raise AuthenticationError("Bearer token is invalid") from None
        if record.revoked_at is not None:
            raise AuthenticationError("This connection has been revoked")
        return record

    def get(self, connection_id: str) -> ConnectionRecord:
        """The current stored record (scopes can change after a provider sign-in)."""
        return self._repository.get_connection(connection_id)

    def by_slug(self, slug: str) -> ConnectionRecord:
        try:
            record = self._repository.find_connection_by_slug(slug)
        except EntityNotFoundError:
            raise EntityNotFoundError("Connector was not found") from None
        if record.revoked_at is not None:
            raise EntityNotFoundError("Connector was not found")
        return record

    def unlink(self, connection_id: str, *, actor: str = "local-administrator") -> ConnectionRecord:
        now = self._clock()
        record = self._repository.revoke_connection(connection_id, now)
        self._oauth.revoke_connection(connection_id, now)
        self._audit(actor, "connection.unlink", record)
        return record

    def rotate_token(
        self, connection_id: str, *, actor: str = "local-administrator"
    ) -> LinkedConnection:
        token = self._token_factory()
        record = self._repository.replace_connection_token(connection_id, hash_token(token))
        # A reset also ends every session Muse obtained through OAuth.
        self._oauth.revoke_connection(connection_id, self._clock())
        self._audit(actor, "connection.rotate_token", record)
        code, expires = self.issue_link_code(connection_id, actor=actor)
        return LinkedConnection(
            self._repository.get_connection(connection_id), token, code, expires
        )

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


def _new_link_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(12))
    return "-".join(raw[index : index + 4] for index in range(0, 12, 4))


def normalize_link_code(value: str) -> str:
    return re.sub(r"[\s-]", "", value).upper()


def _hash_code(code: str, salt: str) -> str:
    return hashlib.scrypt(
        normalize_link_code(code).encode(),
        salt=bytes.fromhex(salt),
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    ).hex()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
