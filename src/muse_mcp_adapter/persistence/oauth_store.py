"""Storage for the authorization server Muse signs in to (clients, codes, tokens)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import cast

from ..domain.errors import ConflictError
from .sqlite_repository import SqliteControlPlaneRepository


@dataclass(frozen=True, slots=True)
class OAuthClient:
    client_id: str
    connection_id: str
    client_name: str
    redirect_uris: tuple[str, ...]
    auth_method: str
    secret_hash: str | None


@dataclass(frozen=True, slots=True)
class AuthRequest:
    request_hash: str
    connection_id: str
    client_id: str
    redirect_uri: str
    state: str | None
    code_challenge: str
    scope: str
    cookie_hash: str | None
    expires_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class AuthCode:
    code_hash: str
    connection_id: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    scope: str
    family_id: str
    expires_at: str
    used_at: str | None


@dataclass(frozen=True, slots=True)
class StoredToken:
    token_hash: str
    kind: str
    connection_id: str
    client_id: str
    scope: str
    family_id: str
    expires_at: str
    used_at: str | None
    revoked_at: str | None


class OAuthServerStore:
    def __init__(self, repository: SqliteControlPlaneRepository) -> None:
        self._repository = repository

    # --- clients -----------------------------------------------------------------------

    def create_client(self, client: OAuthClient, created_at: str) -> None:
        try:
            self._repository.run(
                lambda db: db.execute(
                    "INSERT INTO oauth_clients (client_id, connection_id, client_name, "
                    "redirect_uris_json, auth_method, secret_hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        client.client_id,
                        client.connection_id,
                        client.client_name,
                        json.dumps(list(client.redirect_uris)),
                        client.auth_method,
                        client.secret_hash,
                        created_at,
                    ),
                )
            )
        except Exception as error:
            raise ConflictError("OAuth client could not be created") from error

    def count_clients(self, connection_id: str) -> int:
        row = self._repository.run(
            lambda db: db.execute(
                "SELECT COUNT(*) AS n FROM oauth_clients WHERE connection_id = ?",
                (connection_id,),
            ).fetchone()
        )
        return int(row["n"])

    def get_client(self, client_id: str) -> OAuthClient | None:
        row = self._repository.run(
            lambda db: db.execute(
                "SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)
            ).fetchone()
        )
        if row is None:
            return None
        return OAuthClient(
            client_id=cast(str, row["client_id"]),
            connection_id=cast(str, row["connection_id"]),
            client_name=cast(str, row["client_name"]),
            redirect_uris=tuple(cast(list[str], json.loads(row["redirect_uris_json"]))),
            auth_method=cast(str, row["auth_method"]),
            secret_hash=cast(str | None, row["secret_hash"]),
        )

    # --- authorization requests --------------------------------------------------------

    def create_auth_request(self, request: AuthRequest, created_at: str) -> None:
        self._repository.run(
            lambda db: db.execute(
                "INSERT INTO oauth_auth_requests (request_hash, connection_id, client_id, "
                "redirect_uri, state, code_challenge, scope, cookie_hash, created_at, "
                "expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request.request_hash,
                    request.connection_id,
                    request.client_id,
                    request.redirect_uri,
                    request.state,
                    request.code_challenge,
                    request.scope,
                    request.cookie_hash,
                    created_at,
                    request.expires_at,
                ),
            )
        )

    def get_auth_request(self, request_hash: str) -> AuthRequest | None:
        row = self._repository.run(
            lambda db: db.execute(
                "SELECT * FROM oauth_auth_requests WHERE request_hash = ?", (request_hash,)
            ).fetchone()
        )
        if row is None:
            return None
        return AuthRequest(
            request_hash=cast(str, row["request_hash"]),
            connection_id=cast(str, row["connection_id"]),
            client_id=cast(str, row["client_id"]),
            redirect_uri=cast(str, row["redirect_uri"]),
            state=cast(str | None, row["state"]),
            code_challenge=cast(str, row["code_challenge"]),
            scope=cast(str, row["scope"]),
            cookie_hash=cast(str | None, row["cookie_hash"]),
            expires_at=cast(str, row["expires_at"]),
            completed_at=cast(str | None, row["completed_at"]),
        )

    def bind_auth_request_cookie(self, request_hash: str, cookie_hash: str) -> None:
        self._repository.run(
            lambda db: db.execute(
                "UPDATE oauth_auth_requests SET cookie_hash = ? WHERE request_hash = ?",
                (cookie_hash, request_hash),
            )
        )

    def complete_auth_request(self, request_hash: str, now: str) -> bool:
        """Spend an authorization request exactly once."""
        cursor = self._repository.run(
            lambda db: db.execute(
                "UPDATE oauth_auth_requests SET completed_at = ? "
                "WHERE request_hash = ? AND completed_at IS NULL AND expires_at > ?",
                (now, request_hash, now),
            )
        )
        return cursor.rowcount == 1

    # --- failed consent attempts -------------------------------------------------------

    def record_attempt(self, connection_id: str, at: str) -> None:
        self._repository.run(
            lambda db: db.execute(
                "INSERT INTO oauth_attempts (connection_id, attempted_at) VALUES (?, ?)",
                (connection_id, at),
            )
        )

    def count_attempts_since(self, connection_id: str, since: str) -> int:
        row = self._repository.run(
            lambda db: db.execute(
                "SELECT COUNT(*) AS n FROM oauth_attempts "
                "WHERE connection_id = ? AND attempted_at > ?",
                (connection_id, since),
            ).fetchone()
        )
        return int(row["n"])

    def clear_attempts(self, connection_id: str) -> None:
        self._repository.run(
            lambda db: db.execute(
                "DELETE FROM oauth_attempts WHERE connection_id = ?", (connection_id,)
            )
        )

    # --- authorization codes -----------------------------------------------------------

    def create_code(self, code: AuthCode, created_at: str) -> None:
        self._repository.run(
            lambda db: db.execute(
                "INSERT INTO oauth_codes (code_hash, connection_id, client_id, redirect_uri, "
                "code_challenge, scope, family_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    code.code_hash,
                    code.connection_id,
                    code.client_id,
                    code.redirect_uri,
                    code.code_challenge,
                    code.scope,
                    code.family_id,
                    created_at,
                    code.expires_at,
                ),
            )
        )

    def take_code(self, code_hash: str, now: str) -> tuple[AuthCode | None, bool]:
        """Return the code and whether this call spent it (False when already used)."""

        def operation(db: sqlite3.Connection) -> tuple[AuthCode | None, bool]:
            row = db.execute(
                "SELECT * FROM oauth_codes WHERE code_hash = ?", (code_hash,)
            ).fetchone()
            if row is None:
                return None, False
            code = AuthCode(
                code_hash=row["code_hash"],
                connection_id=row["connection_id"],
                client_id=row["client_id"],
                redirect_uri=row["redirect_uri"],
                code_challenge=row["code_challenge"],
                scope=row["scope"],
                family_id=row["family_id"],
                expires_at=row["expires_at"],
                used_at=row["used_at"],
            )
            cursor = db.execute(
                "UPDATE oauth_codes SET used_at = ? WHERE code_hash = ? AND used_at IS NULL",
                (now, code_hash),
            )
            return code, cursor.rowcount == 1

        return self._repository.run(operation)

    # --- tokens ------------------------------------------------------------------------

    def create_token(self, token: StoredToken, created_at: str) -> None:
        self._repository.run(
            lambda db: db.execute(
                "INSERT INTO oauth_tokens (token_hash, kind, connection_id, client_id, scope, "
                "family_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    token.token_hash,
                    token.kind,
                    token.connection_id,
                    token.client_id,
                    token.scope,
                    token.family_id,
                    created_at,
                    token.expires_at,
                ),
            )
        )

    def get_token(self, token_hash: str) -> StoredToken | None:
        row = self._repository.run(
            lambda db: db.execute(
                "SELECT * FROM oauth_tokens WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        )
        if row is None:
            return None
        return StoredToken(
            token_hash=cast(str, row["token_hash"]),
            kind=cast(str, row["kind"]),
            connection_id=cast(str, row["connection_id"]),
            client_id=cast(str, row["client_id"]),
            scope=cast(str, row["scope"]),
            family_id=cast(str, row["family_id"]),
            expires_at=cast(str, row["expires_at"]),
            used_at=cast(str | None, row["used_at"]),
            revoked_at=cast(str | None, row["revoked_at"]),
        )

    def use_refresh_token(self, token_hash: str, now: str) -> bool:
        """Spend a refresh token once; a second use signals theft."""
        cursor = self._repository.run(
            lambda db: db.execute(
                "UPDATE oauth_tokens SET used_at = ? "
                "WHERE token_hash = ? AND kind = 'refresh' AND used_at IS NULL "
                "AND revoked_at IS NULL",
                (now, token_hash),
            )
        )
        return cursor.rowcount == 1

    def revoke_family(self, family_id: str, now: str) -> None:
        self._repository.run(
            lambda db: db.execute(
                "UPDATE oauth_tokens SET revoked_at = ? WHERE family_id = ? AND revoked_at IS NULL",
                (now, family_id),
            )
        )

    def revoke_connection(self, connection_id: str, now: str) -> None:
        self._repository.run(
            lambda db: db.execute(
                "UPDATE oauth_tokens SET revoked_at = ? WHERE connection_id = ? "
                "AND revoked_at IS NULL",
                (now, connection_id),
            )
        )
