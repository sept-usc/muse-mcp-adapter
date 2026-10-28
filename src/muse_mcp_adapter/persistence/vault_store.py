"""The owner's vault of sealed values, and the ledger of what was released.

A sealed value is stored encrypted, under a handle the agent can hold but not read. The handle is
derived from the value, so reading the same contact twice gives the same handle (the agent can
compare and repeat it) and the vault does not grow with every read. Handles belong to one
connection and expire.

The ledger records what was sealed, withheld, redacted, filled back in or refused, with the handle
and the label but never the value. Each entry carries the hash of the one before it, so an entry
removed or changed afterwards breaks the chain and ``verify`` says where.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from cryptography.fernet import Fernet, InvalidToken

from .sqlite_repository import SqliteControlPlaneRepository

DEFAULT_HANDLE_TTL_SECONDS = 24 * 3600
MASTER_KEY_BYTES = 32
GENESIS = "0" * 64
_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"


class HandleVault:
    def __init__(
        self,
        repository: SqliteControlPlaneRepository,
        master_key: bytes,
        *,
        ttl_seconds: int = DEFAULT_HANDLE_TTL_SECONDS,
    ) -> None:
        if len(master_key) != MASTER_KEY_BYTES:
            raise ValueError(f"The vault key must be {MASTER_KEY_BYTES} bytes")
        self._repository = repository
        self._fernet = Fernet(base64.urlsafe_b64encode(master_key))
        self._id_key = hmac.new(master_key, b"handle-id", hashlib.sha256).digest()
        self._ttl = ttl_seconds

    @staticmethod
    def new_key() -> bytes:
        return base64.urlsafe_b64decode(Fernet.generate_key())

    def handle_for(self, connection_id: str, label: str, value: str, now: str) -> str:
        handle_id = self._handle_id(connection_id, label, value)
        expires = (_time(now) + timedelta(seconds=self._ttl)).isoformat().replace("+00:00", "Z")
        encrypted = self._fernet.encrypt(value.encode())
        self._repository.run(
            lambda db: db.execute(
                "INSERT INTO handles (connection_id, handle_id, label, value_enc, created_at, "
                "expires_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(connection_id, handle_id) "
                "DO UPDATE SET expires_at = excluded.expires_at",
                (connection_id, handle_id, label, encrypted, now, expires),
            )
        )
        return f"[{label}:{handle_id}]"

    def lookup(self, connection_id: str, handle_id: str, now: str) -> tuple[str, str] | None:
        row = self._repository.run(
            lambda db: db.execute(
                "SELECT label, value_enc, expires_at FROM handles "
                "WHERE connection_id = ? AND handle_id = ?",
                (connection_id, handle_id),
            ).fetchone()
        )
        if row is None or _time(cast(str, row["expires_at"])) < _time(now):
            return None
        try:
            value = self._fernet.decrypt(bytes(row["value_enc"])).decode()
        except InvalidToken:
            return None
        return cast(str, row["label"]), value

    def purge_expired(self, now: str) -> int:
        rows = self._repository.run(
            lambda db: db.execute(
                "SELECT connection_id, handle_id, expires_at FROM handles"
            ).fetchall()
        )
        stale = [
            (cast(str, r["connection_id"]), cast(str, r["handle_id"]))
            for r in rows
            if _time(cast(str, r["expires_at"])) < _time(now)
        ]
        for connection_id, handle_id in stale:
            self._repository.run(
                lambda db, c=connection_id, h=handle_id: db.execute(
                    "DELETE FROM handles WHERE connection_id = ? AND handle_id = ?", (c, h)
                )
            )
        return len(stale)

    def revoke_connection(self, connection_id: str) -> None:
        """Forget every sealed value held for a connection, as when it is unlinked."""
        self._repository.run(
            lambda db: db.execute("DELETE FROM handles WHERE connection_id = ?", (connection_id,))
        )

    def _handle_id(self, connection_id: str, label: str, value: str) -> str:
        digest = hmac.new(
            self._id_key, f"{connection_id}\x00{label}\x00{value}".encode(), hashlib.sha256
        ).digest()
        number = int.from_bytes(digest[:5], "big")  # 40 bits: eight base32 characters
        return "".join(_ALPHABET[(number >> shift) & 31] for shift in range(35, -1, -5))


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    seq: int
    occurred_at: str
    connection_id: str
    operation: str
    kind: str
    label: str
    detail: str
    entry_hash: str


class ReleaseLedger:
    def __init__(self, repository: SqliteControlPlaneRepository) -> None:
        self._repository = repository

    def record(
        self, kind: str, *, connection_id: str, operation: str, label: str, detail: str, now: str
    ) -> None:
        def write(db: sqlite3.Connection) -> None:
            last = db.execute(
                "SELECT entry_hash FROM release_ledger ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            previous = cast(str, last["entry_hash"]) if last else GENESIS
            entry = _hash(previous, now, connection_id, operation, kind, label, detail)
            db.execute(
                "INSERT INTO release_ledger (occurred_at, connection_id, operation, kind, label, "
                "detail, prev_hash, entry_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (now, connection_id, operation, kind, label, detail, previous, entry),
            )

        self._repository.run(write)

    def entries(self, *, since: str | None = None) -> list[LedgerEntry]:
        rows = self._repository.run(
            lambda db: db.execute("SELECT * FROM release_ledger ORDER BY seq").fetchall()
        )
        found = [
            LedgerEntry(
                seq=cast(int, r["seq"]),
                occurred_at=cast(str, r["occurred_at"]),
                connection_id=cast(str, r["connection_id"]),
                operation=cast(str, r["operation"]),
                kind=cast(str, r["kind"]),
                label=cast(str, r["label"]),
                detail=cast(str, r["detail"]),
                entry_hash=cast(str, r["entry_hash"]),
            )
            for r in rows
        ]
        if since is None:
            return found
        return [e for e in found if _time(e.occurred_at) >= _time(since)]

    def verify(self) -> tuple[bool, int | None]:
        """Check the whole chain. Returns (ok, the first sequence number that does not fit)."""
        rows = self._repository.run(
            lambda db: db.execute("SELECT * FROM release_ledger ORDER BY seq").fetchall()
        )
        previous = GENESIS
        for r in rows:
            expected = _hash(
                previous,
                cast(str, r["occurred_at"]),
                cast(str, r["connection_id"]),
                cast(str, r["operation"]),
                cast(str, r["kind"]),
                cast(str, r["label"]),
                cast(str, r["detail"]),
            )
            if r["prev_hash"] != previous or r["entry_hash"] != expected:
                return False, cast(int, r["seq"])
            previous = expected
        return True, None

    def summary(self, *, since: str | None = None) -> dict[str, dict[str, int]]:
        """How many of each kind, per label: what the owner reads in a digest."""
        counts: dict[str, dict[str, int]] = {}
        for entry in self.entries(since=since):
            by_label = counts.setdefault(entry.kind, {})
            by_label[entry.label] = by_label.get(entry.label, 0) + 1
        return counts


def _hash(previous: str, *parts: str) -> str:
    payload = json.dumps([previous, *parts], separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


__all__: Sequence[str] = ["HandleVault", "LedgerEntry", "ReleaseLedger"]
