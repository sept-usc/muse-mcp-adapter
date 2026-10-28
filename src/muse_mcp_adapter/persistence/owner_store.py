"""Storage for the owner's side of approvals: the passcode, held actions and known recipients.

An approval request is one held action. It records the exact arguments the owner will be shown,
why it was held, and a one-time link token (only its hash is stored). The owner's decision is bound
to the action's content digest, so approving one action never approves another.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from ..domain.errors import ConflictError
from .sqlite_repository import SqliteControlPlaneRepository

MIN_PASSCODE_LENGTH = 8
MAX_FAILED_ATTEMPTS = 5
FAILURE_WINDOW_SECONDS = 900

PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
USED = "used"

_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    request_id: str
    connection_id: str
    server_id: str
    public_operation_id: str
    classification: str
    content_digest: str
    arguments: object
    reasons: tuple[str, ...]
    recipients: tuple[str, ...]
    status: str
    created_at: str
    expires_at: str
    decided_at: str | None
    decided_by: str | None
    used_at: str | None
    operation_id: str | None

    def expired(self, now: str) -> bool:
        return _time(now) > _time(self.expires_at)


class OwnerStore:
    def __init__(self, repository: SqliteControlPlaneRepository) -> None:
        self._repository = repository

    # --- the owner's passcode ----------------------------------------------------------

    def passcode_is_set(self) -> bool:
        row = self._repository.run(
            lambda db: db.execute("SELECT 1 FROM owner_secret WHERE id = 1").fetchone()
        )
        return row is not None

    def set_passcode(self, passcode: str, now: str) -> None:
        if len(passcode) < MIN_PASSCODE_LENGTH:
            raise ValueError(f"The passcode must be at least {MIN_PASSCODE_LENGTH} characters")
        salt = secrets.token_bytes(16)
        hashed = hashlib.scrypt(passcode.encode(), salt=salt, **_SCRYPT).hex()
        self._repository.run(
            lambda db: db.execute(
                "INSERT INTO owner_secret (id, salt, secret_hash, set_at) VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET salt = excluded.salt, "
                "secret_hash = excluded.secret_hash, set_at = excluded.set_at",
                (salt.hex(), hashed, now),
            )
        )
        self._repository.run(lambda db: db.execute("DELETE FROM owner_failures"))

    def locked(self, now: str) -> bool:
        since = (_time(now) - timedelta(seconds=FAILURE_WINDOW_SECONDS)).isoformat()
        rows = self._repository.run(
            lambda db: db.execute("SELECT occurred_at FROM owner_failures").fetchall()
        )
        recent = sum(1 for row in rows if _time(cast(str, row["occurred_at"])) >= _time(since))
        return recent >= MAX_FAILED_ATTEMPTS

    def check_passcode(self, passcode: str, now: str) -> bool:
        """True for the right passcode. Wrong guesses are counted and lock the page for a while."""
        if self.locked(now):
            return False
        row = self._repository.run(
            lambda db: db.execute(
                "SELECT salt, secret_hash FROM owner_secret WHERE id = 1"
            ).fetchone()
        )
        if row is None:
            return False
        candidate = hashlib.scrypt(
            passcode.encode(), salt=bytes.fromhex(cast(str, row["salt"])), **_SCRYPT
        ).hex()
        if hmac.compare_digest(candidate, cast(str, row["secret_hash"])):
            self._repository.run(lambda db: db.execute("DELETE FROM owner_failures"))
            return True
        self._repository.run(
            lambda db: db.execute("INSERT INTO owner_failures (occurred_at) VALUES (?)", (now,))
        )
        return False

    # --- held actions ------------------------------------------------------------------

    def create_request(
        self,
        *,
        connection_id: str,
        server_id: str,
        public_operation_id: str,
        classification: str,
        content_digest: str,
        arguments: object,
        reasons: Sequence[str],
        recipients: Sequence[str],
        now: str,
        ttl_seconds: int,
    ) -> tuple[ApprovalRequest, str, str]:
        """Store a held action and return it with two one-time link tokens.

        The first is for the agent to show the owner in the chat. The second is for the owner
        alone, sent by a channel the agent is not on, so it never changes while the first may be
        replaced when the agent asks again.
        """
        token = secrets.token_urlsafe(32)
        owner_token = secrets.token_urlsafe(32)
        request_id = secrets.token_hex(8)
        expires = (_time(now) + timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z")
        self._repository.run(
            lambda db: db.execute(
                "INSERT INTO approval_requests (request_id, token_hash, owner_token_hash, "
                "connection_id, server_id, public_operation_id, classification, content_digest, "
                "arguments_json, reasons_json, recipients_json, status, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    request_id,
                    _hash(token),
                    _hash(owner_token),
                    connection_id,
                    server_id,
                    public_operation_id,
                    classification,
                    content_digest,
                    json.dumps(arguments, sort_keys=True),
                    json.dumps(list(reasons)),
                    json.dumps(list(recipients)),
                    now,
                    expires,
                ),
            )
        )
        return self.get(request_id), token, owner_token

    def reissue_token(self, request_id: str) -> str:
        """A fresh one-time link for a request still waiting. The earlier link stops working."""
        token = secrets.token_urlsafe(32)
        self._repository.run(
            lambda db: db.execute(
                "UPDATE approval_requests SET token_hash = ? "
                "WHERE request_id = ? AND status = 'pending'",
                (_hash(token), request_id),
            )
        )
        return token

    def get(self, request_id: str) -> ApprovalRequest:
        row = self._repository.run(
            lambda db: db.execute(
                "SELECT * FROM approval_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        )
        if row is None:
            raise ConflictError("No such approval request")
        return _request(row)

    def by_token(self, token: str) -> ApprovalRequest | None:
        row = self._repository.run(
            lambda db: db.execute(
                "SELECT * FROM approval_requests WHERE token_hash = ? OR owner_token_hash = ?",
                (_hash(token), _hash(token)),
            ).fetchone()
        )
        return None if row is None else _request(row)

    def latest_for(self, connection_id: str, content_digest: str) -> ApprovalRequest | None:
        """The newest request for this exact action, whatever state it is in."""
        row = self._repository.run(
            lambda db: db.execute(
                "SELECT * FROM approval_requests WHERE connection_id = ? AND content_digest = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (connection_id, content_digest),
            ).fetchone()
        )
        return None if row is None else _request(row)

    def list_requests(self, status: str | None = None) -> list[ApprovalRequest]:
        rows = self._repository.run(
            lambda db: db.execute(
                "SELECT * FROM approval_requests"
                + (" WHERE status = ?" if status else "")
                + " ORDER BY created_at, rowid",
                (status,) if status else (),
            ).fetchall()
        )
        return [_request(row) for row in rows]

    def count_pending(self, connection_id: str, now: str) -> int:
        return sum(
            1
            for item in self.list_requests(PENDING)
            if item.connection_id == connection_id and not item.expired(now)
        )

    def decide(self, request_id: str, *, approve: bool, by: str, now: str) -> ApprovalRequest:
        """Record the owner's decision on a request that is still pending and unexpired."""
        current = self.get(request_id)
        if current.status != PENDING or current.expired(now):
            raise ConflictError("This request is no longer waiting for a decision")
        self._repository.run(
            lambda db: db.execute(
                "UPDATE approval_requests SET status = ?, decided_at = ?, decided_by = ? "
                "WHERE request_id = ? AND status = 'pending'",
                (APPROVED if approve else DENIED, now, by, request_id),
            )
        )
        return self.get(request_id)

    def use(self, request_id: str, operation_id: str, now: str) -> None:
        """Spend an approval. It runs exactly one action, once."""

        def spend(db: sqlite3.Connection) -> int:
            cursor = db.execute(
                "UPDATE approval_requests SET status = 'used', used_at = ?, operation_id = ? "
                "WHERE request_id = ? AND status = 'approved'",
                (now, operation_id, request_id),
            )
            return int(cursor.rowcount)

        if self._repository.run(spend) != 1:
            raise ConflictError("This approval has already been used")

    # --- recipients the owner has accepted ---------------------------------------------

    def remember_recipients(self, connection_id: str, values: Sequence[str], now: str) -> None:
        for value in values:
            self._repository.run(
                lambda db, value=value: db.execute(
                    "INSERT OR IGNORE INTO known_recipients (connection_id, value, added_at) "
                    "VALUES (?, ?, ?)",
                    (connection_id, value.lower(), now),
                )
            )

    def known_recipients(self, connection_id: str) -> set[str]:
        rows = self._repository.run(
            lambda db: db.execute(
                "SELECT value FROM known_recipients WHERE connection_id = ?", (connection_id,)
            ).fetchall()
        )
        return {cast(str, row["value"]) for row in rows}

    # --- what happened recently --------------------------------------------------------

    def recently_completed(
        self, connection_id: str, operation_ids: Sequence[str], since: str
    ) -> list[str]:
        """Which of these operations finished for this connection at or after ``since``."""
        if not operation_ids:
            return []
        marks = ",".join("?" for _ in operation_ids)
        rows = self._repository.run(
            lambda db: db.execute(
                "SELECT public_operation_id, updated_at FROM operations WHERE connection_id = ? "
                f"AND status = 'COMPLETED' AND public_operation_id IN ({marks})",
                (connection_id, *operation_ids),
            ).fetchall()
        )
        cutoff = _time(since)
        found = {
            cast(str, row["public_operation_id"])
            for row in rows
            if _time(cast(str, row["updated_at"])) >= cutoff
        }
        return sorted(found)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _request(row: sqlite3.Row) -> ApprovalRequest:
    data = cast("dict[str, object]", dict(row))
    return ApprovalRequest(
        request_id=cast(str, data["request_id"]),
        connection_id=cast(str, data["connection_id"]),
        server_id=cast(str, data["server_id"]),
        public_operation_id=cast(str, data["public_operation_id"]),
        classification=cast(str, data["classification"]),
        content_digest=cast(str, data["content_digest"]),
        arguments=json.loads(cast(str, data["arguments_json"])),
        reasons=tuple(json.loads(cast(str, data["reasons_json"]))),
        recipients=tuple(json.loads(cast(str, data["recipients_json"]))),
        status=cast(str, data["status"]),
        created_at=cast(str, data["created_at"]),
        expires_at=cast(str, data["expires_at"]),
        decided_at=cast("str | None", data["decided_at"]),
        decided_by=cast("str | None", data["decided_by"]),
        used_at=cast("str | None", data["used_at"]),
        operation_id=cast("str | None", data["operation_id"]),
    )
