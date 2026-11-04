"""Storage for chat-driven setup: proposals, the owner's credential, sessions and provenance."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from ..catalog.canonical import thaw_json
from ..domain.errors import ConflictError, EntityNotFoundError
from ..persistence.sqlite_repository import SqliteControlPlaneRepository
from .models import OPEN_STATES
from .secrets import SecretBox

_APP_PREFIX = "OAUTH_APP:"
_JSON_FIELDS = {
    "request": "request_json",
    "computed": "computed_json",
    "decision": "decision_json",
    "applied": "applied_json",
}
_PLAIN_FIELDS = {
    "target_server_id": "target_server_id",
    "target_connection_id": "target_connection_id",
    "snapshot_id": "snapshot_id",
    "digest": "digest",
    "error": "error",
}


@dataclass(frozen=True, slots=True)
class Proposal:
    proposal_id: str
    requester_connection_id: str
    kind: str
    state: str
    user_words: str
    request: Mapping[str, object]
    target_server_id: str | None
    target_connection_id: str | None
    snapshot_id: int | None
    computed: Mapping[str, object] | None
    digest: str | None
    decision: Mapping[str, object] | None
    applied: Mapping[str, object] | None
    has_reveal: bool
    error: str | None
    created_at: str
    updated_at: str
    expires_at: str

    def expired(self, now: str) -> bool:
        return self.expires_at <= now


class SetupStore:
    def __init__(self, repository: SqliteControlPlaneRepository, box: SecretBox) -> None:
        self._repository = repository
        self._box = box

    # --- proposals ---------------------------------------------------------------------

    def create_proposal(
        self,
        *,
        proposal_id: str,
        requester_connection_id: str,
        kind: str,
        user_words: str,
        request: Mapping[str, object],
        token_hash: str,
        now: str,
        expires_at: str,
        target_server_id: str | None = None,
        target_connection_id: str | None = None,
    ) -> Proposal:
        self._repository.run(
            lambda db: db.execute(
                "INSERT INTO setup_proposals (proposal_id, requester_connection_id, kind, state, "
                "token_hash, user_words, request_json, target_server_id, target_connection_id, "
                "created_at, updated_at, expires_at) "
                "VALUES (?, ?, ?, 'waiting_for_owner', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    proposal_id,
                    requester_connection_id,
                    kind,
                    token_hash,
                    user_words,
                    _dump(request),
                    target_server_id,
                    target_connection_id,
                    now,
                    now,
                    expires_at,
                ),
            )
        )
        return self.get_proposal(proposal_id)

    def get_proposal(self, proposal_id: str) -> Proposal:
        row = self._repository.run(
            lambda db: db.execute(
                "SELECT * FROM setup_proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        )
        if row is None:
            raise EntityNotFoundError("That proposal does not exist")
        return _proposal(row)

    def find_by_token(self, token_hash: str) -> Proposal | None:
        row = self._repository.run(
            lambda db: db.execute(
                "SELECT * FROM setup_proposals WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        )
        return _proposal(row) if row is not None else None

    def list_proposals(
        self, *, states: Sequence[str] | None = None, limit: int = 100
    ) -> list[Proposal]:
        query = "SELECT * FROM setup_proposals"
        parameters: list[object] = []
        if states:
            query += " WHERE state IN (" + ",".join("?" for _ in states) + ")"
            parameters.extend(states)
        query += " ORDER BY created_at DESC, proposal_id LIMIT ?"
        parameters.append(limit)
        rows = self._repository.run(lambda db: db.execute(query, parameters).fetchall())
        return [_proposal(row) for row in rows]

    def count_open(self, connection_id: str) -> int:
        marks = ",".join("?" for _ in OPEN_STATES)
        row = self._repository.run(
            lambda db: db.execute(
                "SELECT COUNT(*) AS n FROM setup_proposals "
                f"WHERE requester_connection_id = ? AND state IN ({marks})",
                (connection_id, *OPEN_STATES),
            ).fetchone()
        )
        return int(row["n"])

    def find_open_duplicate(
        self, connection_id: str, kind: str, request: Mapping[str, object]
    ) -> Proposal | None:
        marks = ",".join("?" for _ in OPEN_STATES)
        rows = self._repository.run(
            lambda db: db.execute(
                "SELECT * FROM setup_proposals WHERE requester_connection_id = ? AND kind = ? "
                f"AND request_json = ? AND state IN ({marks}) ORDER BY created_at DESC LIMIT 1",
                (connection_id, kind, _dump(request), *OPEN_STATES),
            ).fetchall()
        )
        return _proposal(rows[0]) if rows else None

    def transition(
        self,
        proposal_id: str,
        *,
        expected: Collection[str],
        state: str,
        now: str,
        reveal: str | None | bool = False,
        **fields: object,
    ) -> Proposal:
        """Move a proposal to a new state only if it is still in an expected one.

        ``reveal`` is False to leave the sealed connector token alone, None to wipe it, or the
        plain text to seal and keep.
        """
        assignments = ["state = ?", "updated_at = ?"]
        values: list[object] = [state, now]
        for name, value in fields.items():
            if name in _JSON_FIELDS:
                assignments.append(f"{_JSON_FIELDS[name]} = ?")
                values.append(_dump(value) if value is not None else None)
            elif name in _PLAIN_FIELDS:
                assignments.append(f"{_PLAIN_FIELDS[name]} = ?")
                values.append(value)
            else:
                raise ValueError(f"Unknown proposal field {name}")
        if reveal is None:
            assignments.append("reveal_ciphertext = NULL")
        elif isinstance(reveal, str):
            assignments.append("reveal_ciphertext = ?")
            values.append(self._box.seal(reveal))
        marks = ",".join("?" for _ in expected)
        values.extend([proposal_id, *expected])

        def run(db: sqlite3.Connection) -> int:
            return db.execute(
                f"UPDATE setup_proposals SET {', '.join(assignments)} "
                f"WHERE proposal_id = ? AND state IN ({marks})",
                values,
            ).rowcount

        if self._repository.run(run) != 1:
            raise ConflictError("This proposal is no longer in the state this step needs")
        return self.get_proposal(proposal_id)

    def reissue_token(self, proposal_id: str, token_hash: str, now: str) -> None:
        marks = ",".join("?" for _ in OPEN_STATES)

        def run(db: sqlite3.Connection) -> int:
            return db.execute(
                "UPDATE setup_proposals SET token_hash = ?, updated_at = ? "
                f"WHERE proposal_id = ? AND state IN ({marks})",
                (token_hash, now, proposal_id, *OPEN_STATES),
            ).rowcount

        if self._repository.run(run) != 1:
            raise ConflictError("Only an open proposal can get a new link")

    def expire_due(self, now: str) -> list[Proposal]:
        marks = ",".join("?" for _ in OPEN_STATES)
        due = self._repository.run(
            lambda db: db.execute(
                f"SELECT * FROM setup_proposals WHERE state IN ({marks}) AND expires_at <= ?",
                (*OPEN_STATES, now),
            ).fetchall()
        )
        expired: list[Proposal] = []
        for row in due:
            proposal = _proposal(row)
            try:
                expired.append(
                    self.transition(
                        proposal.proposal_id,
                        expected=OPEN_STATES,
                        state="expired",
                        now=now,
                        reveal=None,
                    )
                )
            except ConflictError:
                continue
        return expired

    def wipe_old_reveals(self, before: str) -> None:
        """Forget connector tokens that were never collected, once their time is up."""
        self._repository.run(
            lambda db: db.execute(
                "UPDATE setup_proposals SET reveal_ciphertext = NULL "
                "WHERE reveal_ciphertext IS NOT NULL "
                "AND state IN ('applied', 'declined', 'expired', 'failed') AND updated_at <= ?",
                (before,),
            )
        )

    def reveal_text(self, proposal_id: str) -> str | None:
        row = self._repository.run(
            lambda db: db.execute(
                "SELECT reveal_ciphertext FROM setup_proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        )
        sealed = cast(str | None, row["reveal_ciphertext"]) if row is not None else None
        return self._box.open(sealed) if sealed else None

    # --- the owner's credential, sessions and attempts ---------------------------------

    def set_owner_code(self, code_hash: str, salt: str, now: str) -> None:
        self._repository.run(
            lambda db: db.execute(
                "INSERT INTO owner_credential (id, code_hash, code_salt, created_at) "
                "VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "code_hash = excluded.code_hash, code_salt = excluded.code_salt, "
                "created_at = excluded.created_at",
                (code_hash, salt, now),
            )
        )

    def owner_code(self) -> tuple[str, str] | None:
        row = self._repository.run(
            lambda db: db.execute("SELECT code_hash, code_salt FROM owner_credential").fetchone()
        )
        if row is None:
            return None
        return cast(str, row["code_hash"]), cast(str, row["code_salt"])

    def add_session(self, session_hash: str, now: str, expires_at: str) -> None:
        self._repository.run(
            lambda db: db.execute(
                "INSERT INTO owner_sessions (session_hash, created_at, expires_at) "
                "VALUES (?, ?, ?)",
                (session_hash, now, expires_at),
            )
        )

    def session_valid(self, session_hash: str, now: str) -> bool:
        row = self._repository.run(
            lambda db: db.execute(
                "SELECT 1 FROM owner_sessions WHERE session_hash = ? AND expires_at > ?",
                (session_hash, now),
            ).fetchone()
        )
        return row is not None

    def drop_sessions(self, now: str | None = None) -> None:
        """Remove expired sessions, or every session when no time is given."""
        if now is None:
            self._repository.run(lambda db: db.execute("DELETE FROM owner_sessions"))
        else:
            self._repository.run(
                lambda db: db.execute("DELETE FROM owner_sessions WHERE expires_at <= ?", (now,))
            )

    def add_attempt(self, scope: str, now: str) -> None:
        self._repository.run(
            lambda db: db.execute(
                "INSERT INTO owner_attempts (scope, attempted_at) VALUES (?, ?)", (scope, now)
            )
        )

    def count_attempts(self, scope: str, since: str) -> int:
        row = self._repository.run(
            lambda db: db.execute(
                "SELECT COUNT(*) AS n FROM owner_attempts WHERE scope = ? AND attempted_at > ?",
                (scope, since),
            ).fetchone()
        )
        return int(row["n"])

    def clear_attempts(self, scope: str) -> None:
        self._repository.run(
            lambda db: db.execute("DELETE FROM owner_attempts WHERE scope = ?", (scope,))
        )

    # --- secrets the owner typed on a page ---------------------------------------------

    def put_secret(self, name: str, value: str, now: str) -> None:
        sealed = self._box.seal(value)
        self._repository.run(
            lambda db: db.execute(
                "INSERT INTO owner_secrets (name, ciphertext, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET ciphertext = excluded.ciphertext, "
                "created_at = excluded.created_at",
                (name, sealed, now),
            )
        )

    def get_secret(self, name: str) -> str | None:
        row = self._repository.run(
            lambda db: db.execute(
                "SELECT ciphertext FROM owner_secrets WHERE name = ?", (name,)
            ).fetchone()
        )
        return self._box.open(cast(str, row["ciphertext"])) if row is not None else None

    def delete_secret(self, name: str) -> None:
        self._repository.run(
            lambda db: db.execute("DELETE FROM owner_secrets WHERE name = ?", (name,))
        )

    # --- the OAuth app the owner registered with a service ------------------------------

    def put_oauth_client(
        self, connection_id: str, client_id: str, client_secret: str | None, now: str
    ) -> None:
        body = json.dumps({"clientId": client_id, "clientSecret": client_secret})
        self.put_secret(_APP_PREFIX + connection_id, body, now)

    def oauth_client(self, connection_id: str) -> tuple[str, str | None] | None:
        text = self.get_secret(_APP_PREFIX + connection_id)
        if text is None:
            return None
        try:
            data = cast(Mapping[str, object], json.loads(text))
        except ValueError:
            return None
        client_id, secret = data.get("clientId"), data.get("clientSecret")
        if not isinstance(client_id, str) or not client_id:
            return None
        return client_id, secret if isinstance(secret, str) and secret else None

    def delete_oauth_client(self, connection_id: str) -> None:
        self.delete_secret(_APP_PREFIX + connection_id)

    # --- provenance --------------------------------------------------------------------

    def record_provenance(
        self,
        *,
        server_id: str,
        catalog_version: str,
        origin: str,
        proposal_id: str | None,
        proposal_digest: str | None,
        now: str,
    ) -> None:
        self._repository.run(
            lambda db: db.execute(
                "INSERT OR REPLACE INTO catalog_provenance (server_id, catalog_version, origin, "
                "proposal_id, proposal_digest, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                (server_id, catalog_version, origin, proposal_id, proposal_digest, now),
            )
        )

    def provenance(self, server_id: str, catalog_version: str) -> Mapping[str, object] | None:
        row = self._repository.run(
            lambda db: db.execute(
                "SELECT * FROM catalog_provenance WHERE server_id = ? AND catalog_version = ?",
                (server_id, catalog_version),
            ).fetchone()
        )
        if row is None:
            return None
        return {
            "serverId": row["server_id"],
            "catalogVersion": row["catalog_version"],
            "origin": row["origin"],
            "proposalId": row["proposal_id"],
            "proposalDigest": row["proposal_digest"],
            "recordedAt": row["recorded_at"],
        }


def _dump(value: object) -> str:
    return json.dumps(thaw_json(value), separators=(",", ":"), sort_keys=True)


def _load(value: str | None) -> Mapping[str, object] | None:
    if value is None:
        return None
    loaded: object = json.loads(value)
    return cast(Mapping[str, object], loaded) if isinstance(loaded, dict) else None


def _proposal(row: sqlite3.Row) -> Proposal:
    return Proposal(
        proposal_id=cast(str, row["proposal_id"]),
        requester_connection_id=cast(str, row["requester_connection_id"]),
        kind=cast(str, row["kind"]),
        state=cast(str, row["state"]),
        user_words=cast(str, row["user_words"]),
        request=_load(cast(str, row["request_json"])) or {},
        target_server_id=cast(str | None, row["target_server_id"]),
        target_connection_id=cast(str | None, row["target_connection_id"]),
        snapshot_id=cast(int | None, row["snapshot_id"]),
        computed=_load(cast(str | None, row["computed_json"])),
        digest=cast(str | None, row["digest"]),
        decision=_load(cast(str | None, row["decision_json"])),
        applied=_load(cast(str | None, row["applied_json"])),
        has_reveal=row["reveal_ciphertext"] is not None,
        error=cast(str | None, row["error"]),
        created_at=cast(str, row["created_at"]),
        updated_at=cast(str, row["updated_at"]),
        expires_at=cast(str, row["expires_at"]),
    )
