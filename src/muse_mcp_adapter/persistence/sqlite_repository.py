import functools
import hashlib
import hmac
import json
import os
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Self, cast

from ..catalog.canonical import freeze_json, thaw_json
from ..catalog.serde import catalog_from_dict, catalog_to_dict
from ..domain.errors import ConflictError, EntityNotFoundError, InvalidStateError
from ..domain.models import (
    ArtifactRecord,
    CatalogActivation,
    DiscoverySnapshot,
    OperationEvent,
    OperationRecord,
    PublishedCatalog,
    ServerRegistration,
)

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS server_registrations (
    server_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    auth_profile TEXT NOT NULL,
    provider_pack TEXT,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discovery_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id TEXT NOT NULL REFERENCES server_registrations(server_id),
    protocol_version TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL,
    server_info_json TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    primitives_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS discovery_by_server
    ON discovery_snapshots(server_id, snapshot_id DESC);

CREATE TABLE IF NOT EXISTS published_catalogs (
    server_id TEXT NOT NULL REFERENCES server_registrations(server_id),
    catalog_version TEXT NOT NULL,
    catalog_digest TEXT NOT NULL,
    published_at TEXT NOT NULL,
    catalog_json TEXT NOT NULL,
    signature TEXT,
    PRIMARY KEY (server_id, catalog_version),
    UNIQUE (server_id, catalog_digest)
);

CREATE TABLE IF NOT EXISTS active_catalogs (
    server_id TEXT PRIMARY KEY REFERENCES server_registrations(server_id),
    catalog_version TEXT NOT NULL,
    activated_at TEXT NOT NULL,
    FOREIGN KEY (server_id, catalog_version)
        REFERENCES published_catalogs(server_id, catalog_version)
);

CREATE TABLE IF NOT EXISTS catalog_activation_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id TEXT NOT NULL REFERENCES server_registrations(server_id),
    event_type TEXT NOT NULL CHECK (event_type IN ('activate', 'rollback')),
    from_version TEXT,
    to_version TEXT NOT NULL,
    activated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    server_id TEXT NOT NULL REFERENCES server_registrations(server_id),
    catalog_version TEXT NOT NULL,
    public_operation_id TEXT NOT NULL,
    local_identity_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    classification TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT,
    request_digest TEXT NOT NULL,
    approval_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    result_json TEXT,
    error_code TEXT,
    error_message TEXT,
    retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0, 1)),
    UNIQUE (
        server_id, public_operation_id, local_identity_id,
        connection_id, idempotency_key
    )
);

CREATE INDEX IF NOT EXISTS operations_by_status
    ON operations(server_id, status, updated_at);

CREATE TABLE IF NOT EXISTS operation_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    data_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS operation_events_by_operation
    ON operation_events(operation_id, event_id);

CREATE TABLE IF NOT EXISTS approval_consumptions (
    approval_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    consumed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_audit (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    server_id TEXT,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_audit (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    request_id TEXT NOT NULL,
    server_id TEXT NOT NULL,
    public_operation_id TEXT NOT NULL,
    local_identity_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    catalog_version TEXT,
    classification TEXT,
    approval_id TEXT,
    outcome TEXT NOT NULL,
    error_code TEXT,
    duration_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    file_name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    content_digest TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


_ALLOWED_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "PENDING": frozenset({"APPROVAL_REQUIRED", "RUNNING", "FAILED", "CANCELLED"}),
    "APPROVAL_REQUIRED": frozenset({"RUNNING", "FAILED", "CANCELLED"}),
    "RUNNING": frozenset({"COMPLETED", "FAILED", "UNKNOWN", "INPUT_REQUIRED", "CANCELLED"}),
    "INPUT_REQUIRED": frozenset({"RUNNING", "FAILED", "CANCELLED"}),
    "UNKNOWN": frozenset({"COMPLETED", "FAILED"}),
    "FAILED": frozenset({"RUNNING"}),
    "COMPLETED": frozenset(),
    "CANCELLED": frozenset(),
}


def _locked(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    def wrapper(self: "SqliteControlPlaneRepository", *args: Any, **kwargs: Any) -> Any:
        with self._lock:  # pyright: ignore[reportPrivateUsage]
            return method(self, *args, **kwargs)

    return wrapper


class SqliteControlPlaneRepository:
    """SQLite-backed store. All public methods are serialized so one instance is thread-safe."""

    def __init__(self, database: str | Path, *, integrity_key: bytes | None = None) -> None:
        self._lock = threading.RLock()
        self._integrity_key = integrity_key
        is_file = str(database) != ":memory:"
        if is_file:
            fd = os.open(str(database), os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        if is_file:
            self._connection.execute("PRAGMA journal_mode = WAL")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def initialize(self) -> None:
        with self._connection:
            self._connection.executescript(_SCHEMA)
            columns = {
                str(row["name"])
                for row in self._connection.execute("PRAGMA table_info(published_catalogs)")
            }
            if "signature" not in columns:
                self._connection.execute(
                    "ALTER TABLE published_catalogs ADD COLUMN signature TEXT"
                )

    def _sign(self, payload: str) -> str | None:
        if self._integrity_key is None:
            return None
        return hmac.new(self._integrity_key, payload.encode(), hashlib.sha256).hexdigest()

    def close(self) -> None:
        self._connection.close()

    def create_server(self, server: ServerRegistration) -> ServerRegistration:
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO server_registrations (
                        server_id, display_name, endpoint, auth_profile,
                        provider_pack, enabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        server.server_id,
                        server.display_name,
                        server.endpoint,
                        server.auth_profile,
                        server.provider_pack,
                        int(server.enabled),
                        server.created_at,
                        server.updated_at,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"Server {server.server_id} already exists") from error
        return server

    def get_server(self, server_id: str) -> ServerRegistration:
        row = self._connection.execute(
            "SELECT * FROM server_registrations WHERE server_id = ?", (server_id,)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"Server {server_id} was not found")
        return _server_from_row(row)

    def set_server_enabled(
        self, server_id: str, enabled: bool, updated_at: str
    ) -> ServerRegistration:
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE server_registrations SET enabled = ?, updated_at = ? WHERE server_id = ?",
                (int(enabled), updated_at, server_id),
            )
        if cursor.rowcount != 1:
            raise EntityNotFoundError(f"Server {server_id} was not found")
        return self.get_server(server_id)

    def list_servers(self) -> tuple[ServerRegistration, ...]:
        rows = self._connection.execute(
            "SELECT * FROM server_registrations ORDER BY server_id"
        ).fetchall()
        return tuple(_server_from_row(row) for row in rows)

    def save_discovery(self, snapshot: DiscoverySnapshot) -> DiscoverySnapshot:
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO discovery_snapshots (
                    server_id, protocol_version, discovered_at, snapshot_digest,
                    server_info_json, capabilities_json, primitives_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.server_id,
                    snapshot.protocol_version,
                    snapshot.discovered_at,
                    snapshot.snapshot_digest,
                    _json(snapshot.server_info),
                    _json(snapshot.capabilities),
                    _json(snapshot.primitives),
                ),
            )
        return DiscoverySnapshot(
            snapshot_id=cursor.lastrowid,
            server_id=snapshot.server_id,
            protocol_version=snapshot.protocol_version,
            discovered_at=snapshot.discovered_at,
            snapshot_digest=snapshot.snapshot_digest,
            server_info=snapshot.server_info,
            capabilities=snapshot.capabilities,
            primitives=snapshot.primitives,
        )

    def get_latest_discovery(self, server_id: str) -> DiscoverySnapshot:
        row = self._connection.execute(
            """
            SELECT * FROM discovery_snapshots
            WHERE server_id = ?
            ORDER BY snapshot_id DESC
            LIMIT 1
            """,
            (server_id,),
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"No discovery snapshot exists for server {server_id}")
        return _snapshot_from_row(row)

    def save_catalog(self, catalog: PublishedCatalog) -> PublishedCatalog:
        server_id = catalog.server.get("id")
        if not isinstance(server_id, str):
            raise InvalidStateError("Catalog server ID is missing")
        catalog_json = json.dumps(catalog_to_dict(catalog), separators=(",", ":"))
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO published_catalogs (
                        server_id, catalog_version, catalog_digest, published_at,
                        catalog_json, signature
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        server_id,
                        catalog.catalog_version,
                        catalog.catalog_digest,
                        catalog.published_at,
                        catalog_json,
                        self._sign(catalog_json),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError(
                f"Catalog {server_id}/{catalog.catalog_version} already exists"
            ) from error
        return catalog

    def get_catalog(self, server_id: str, catalog_version: str) -> PublishedCatalog:
        row = self._connection.execute(
            """
            SELECT catalog_json, signature FROM published_catalogs
            WHERE server_id = ? AND catalog_version = ?
            """,
            (server_id, catalog_version),
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"Catalog {server_id}/{catalog_version} was not found")
        catalog_json = cast(str, row["catalog_json"])
        if self._integrity_key is not None:
            expected = self._sign(catalog_json)
            stored = cast(str | None, row["signature"])
            if stored is None or expected is None or not hmac.compare_digest(stored, expected):
                raise InvalidStateError("Stored catalog failed its integrity signature check")
        return catalog_from_dict(_load_mapping(catalog_json))

    def activate_catalog(
        self, server_id: str, catalog_version: str, activated_at: str
    ) -> CatalogActivation:
        self.get_catalog(server_id, catalog_version)
        previous = self._active_version(server_id)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO active_catalogs (server_id, catalog_version, activated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(server_id) DO UPDATE SET
                    catalog_version = excluded.catalog_version,
                    activated_at = excluded.activated_at
                """,
                (server_id, catalog_version, activated_at),
            )
            self._connection.execute(
                """
                INSERT INTO catalog_activation_events (
                    server_id, event_type, from_version, to_version, activated_at
                ) VALUES (?, 'activate', ?, ?, ?)
                """,
                (server_id, previous, catalog_version, activated_at),
            )
        return CatalogActivation(
            server_id=server_id,
            catalog_version=catalog_version,
            previous_catalog_version=previous,
            event_type="activate",
            activated_at=activated_at,
        )

    def get_active_catalog(self, server_id: str) -> PublishedCatalog:
        version = self._active_version(server_id)
        if version is None:
            raise EntityNotFoundError(f"Server {server_id} has no active catalog")
        return self.get_catalog(server_id, version)

    def rollback_catalog(self, server_id: str, activated_at: str) -> CatalogActivation:
        current = self._active_version(server_id)
        if current is None:
            raise InvalidStateError(f"Server {server_id} has no active catalog to roll back")

        row = self._connection.execute(
            """
            SELECT from_version FROM catalog_activation_events
            WHERE server_id = ? AND event_type = 'activate' AND to_version = ?
            ORDER BY event_id DESC
            LIMIT 1
            """,
            (server_id, current),
        ).fetchone()
        previous = cast(str | None, row["from_version"]) if row is not None else None
        if previous is None:
            raise InvalidStateError(f"Catalog {server_id}/{current} has no rollback target")

        self.get_catalog(server_id, previous)
        with self._connection:
            self._connection.execute(
                """
                UPDATE active_catalogs
                SET catalog_version = ?, activated_at = ?
                WHERE server_id = ?
                """,
                (previous, activated_at, server_id),
            )
            self._connection.execute(
                """
                INSERT INTO catalog_activation_events (
                    server_id, event_type, from_version, to_version, activated_at
                ) VALUES (?, 'rollback', ?, ?, ?)
                """,
                (server_id, current, previous, activated_at),
            )
        return CatalogActivation(
            server_id=server_id,
            catalog_version=previous,
            previous_catalog_version=current,
            event_type="rollback",
            activated_at=activated_at,
        )

    def create_operation(self, operation: OperationRecord) -> OperationRecord:
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO operations (
                        operation_id, request_id, server_id, catalog_version,
                        public_operation_id, local_identity_id, connection_id,
                        classification, status, idempotency_key, request_digest,
                        approval_id, created_at, updated_at, result_json,
                        error_code, error_message, retryable
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        operation.operation_id,
                        operation.request_id,
                        operation.server_id,
                        operation.catalog_version,
                        operation.public_operation_id,
                        operation.local_identity_id,
                        operation.connection_id,
                        operation.classification,
                        operation.status,
                        operation.idempotency_key,
                        operation.request_digest,
                        operation.approval_id,
                        operation.created_at,
                        operation.updated_at,
                        _json(operation.result) if operation.result is not None else None,
                        operation.error_code,
                        operation.error_message,
                        int(operation.retryable),
                    ),
                )
                self._connection.execute(
                    """
                    INSERT INTO operation_events (
                        operation_id, event_type, occurred_at, data_json
                    ) VALUES (?, 'state_changed', ?, ?)
                    """,
                    (
                        operation.operation_id,
                        operation.created_at,
                        _json({"status": operation.status}),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError("Operation or idempotency key already exists") from error
        return operation

    def get_operation(self, operation_id: str) -> OperationRecord:
        row = self._connection.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"Operation {operation_id} was not found")
        return _operation_from_row(row)

    def find_operation_by_idempotency(
        self,
        *,
        server_id: str,
        public_operation_id: str,
        local_identity_id: str,
        connection_id: str,
        idempotency_key: str,
    ) -> OperationRecord | None:
        row = self._connection.execute(
            """
            SELECT * FROM operations
            WHERE server_id = ?
              AND public_operation_id = ?
              AND local_identity_id = ?
              AND connection_id = ?
              AND idempotency_key = ?
            """,
            (
                server_id,
                public_operation_id,
                local_identity_id,
                connection_id,
                idempotency_key,
            ),
        ).fetchone()
        return _operation_from_row(row) if row is not None else None

    def transition_operation(
        self,
        operation_id: str,
        *,
        expected_statuses: tuple[str, ...],
        status: str,
        updated_at: str,
        approval_id: str | None = None,
        result: Mapping[str, object] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        retryable: bool = False,
    ) -> OperationRecord:
        if not expected_statuses:
            raise InvalidStateError("At least one expected operation status is required")
        for source in expected_statuses:
            if status not in _ALLOWED_TRANSITIONS.get(source, frozenset()):
                raise InvalidStateError(f"Operation transition {source} -> {status} is not allowed")
        placeholders = ",".join("?" for _ in expected_statuses)
        parameters: list[object] = [
            status,
            updated_at,
            approval_id,
            _json(result) if result is not None else None,
            error_code,
            error_message,
            int(retryable),
            operation_id,
            *expected_statuses,
        ]
        with self._connection:
            cursor = self._connection.execute(
                f"""
                UPDATE operations
                SET status = ?,
                    updated_at = ?,
                    approval_id = COALESCE(?, approval_id),
                    result_json = ?,
                    error_code = ?,
                    error_message = ?,
                    retryable = ?
                WHERE operation_id = ?
                  AND status IN ({placeholders})
                """,
                parameters,
            )
            if cursor.rowcount == 1:
                self._connection.execute(
                    """
                    INSERT INTO operation_events (
                        operation_id, event_type, occurred_at, data_json
                    ) VALUES (?, 'state_changed', ?, ?)
                    """,
                    (operation_id, updated_at, _json({"status": status})),
                )
        if cursor.rowcount != 1:
            current = self.get_operation(operation_id)
            raise InvalidStateError(
                f"Operation {operation_id} cannot transition from {current.status} to {status}"
            )
        return self.get_operation(operation_id)

    def append_operation_event(
        self,
        operation_id: str,
        *,
        event_type: str,
        occurred_at: str,
        data: Mapping[str, object],
    ) -> OperationEvent:
        self.get_operation(operation_id)
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO operation_events (
                    operation_id, event_type, occurred_at, data_json
                ) VALUES (?, ?, ?, ?)
                """,
                (operation_id, event_type, occurred_at, _json(data)),
            )
        return OperationEvent(
            event_id=cursor.lastrowid,
            operation_id=operation_id,
            event_type=event_type,
            occurred_at=occurred_at,
            data=cast(Mapping[str, object], freeze_json(data)),
        )

    def list_operation_events(self, operation_id: str) -> tuple[OperationEvent, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM operation_events
            WHERE operation_id = ? ORDER BY event_id
            """,
            (operation_id,),
        ).fetchall()
        return tuple(
            OperationEvent(
                event_id=cast(int, row["event_id"]),
                operation_id=cast(str, row["operation_id"]),
                event_type=cast(str, row["event_type"]),
                occurred_at=cast(str, row["occurred_at"]),
                data=cast(
                    Mapping[str, object],
                    freeze_json(_load_mapping(cast(str, row["data_json"]))),
                ),
            )
            for row in rows
        )

    def list_stale_operations(
        self, *, status: str, updated_before: str
    ) -> tuple[OperationRecord, ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM operations
            WHERE status = ? AND updated_at < ?
            ORDER BY updated_at, operation_id
            """,
            (status, updated_before),
        ).fetchall()
        return tuple(_operation_from_row(row) for row in rows)

    def count_operation_events(self, operation_id: str, event_type: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS n FROM operation_events WHERE operation_id = ? AND event_type = ?",
            (operation_id, event_type),
        ).fetchone()
        return int(row["n"])

    def consume_approval(self, approval_id: str, operation_id: str, consumed_at: str) -> None:
        """Bind an approval to exactly one operation; re-use by the same operation is allowed."""
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO approval_consumptions "
                "(approval_id, operation_id, consumed_at) VALUES (?, ?, ?)",
                (approval_id, operation_id, consumed_at),
            )
            row = self._connection.execute(
                "SELECT operation_id FROM approval_consumptions WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        if row is None or row["operation_id"] != operation_id:
            raise ConflictError("Approval has already been used for another operation")

    def record_admin_audit(
        self,
        *,
        occurred_at: str,
        actor: str,
        action: str,
        server_id: str | None,
        data: Mapping[str, object],
    ) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO admin_audit (occurred_at, actor, action, server_id, data_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (occurred_at, actor, action, server_id, _json(data)),
            )

    def list_admin_audit(self, server_id: str | None = None) -> tuple[Mapping[str, object], ...]:
        query = "SELECT * FROM admin_audit"
        parameters: tuple[object, ...] = ()
        if server_id is not None:
            query += " WHERE server_id = ?"
            parameters = (server_id,)
        rows = self._connection.execute(query + " ORDER BY event_id", parameters).fetchall()
        return tuple(
            {
                "occurredAt": row["occurred_at"],
                "actor": row["actor"],
                "action": row["action"],
                "serverId": row["server_id"],
                "data": json.loads(cast(str, row["data_json"])),
            }
            for row in rows
        )

    def record_runtime_audit(
        self,
        *,
        occurred_at: str,
        request_id: str,
        server_id: str,
        public_operation_id: str,
        local_identity_id: str,
        connection_id: str,
        catalog_version: str | None,
        classification: str | None,
        approval_id: str | None,
        outcome: str,
        error_code: str | None,
        duration_ms: int,
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO runtime_audit (
                    occurred_at, request_id, server_id, public_operation_id,
                    local_identity_id, connection_id, catalog_version, classification,
                    approval_id, outcome, error_code, duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    occurred_at,
                    request_id,
                    server_id,
                    public_operation_id,
                    local_identity_id,
                    connection_id,
                    catalog_version,
                    classification,
                    approval_id,
                    outcome,
                    error_code,
                    duration_ms,
                ),
            )

    def list_runtime_audit(self) -> tuple[Mapping[str, object], ...]:
        rows = self._connection.execute("SELECT * FROM runtime_audit ORDER BY event_id").fetchall()
        return tuple(dict(row) for row in rows)

    def save_artifact(self, artifact: ArtifactRecord) -> ArtifactRecord:
        self.get_operation(artifact.operation_id)
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO artifacts (
                        artifact_id, operation_id, file_name, media_type,
                        size_bytes, content_digest, storage_path, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.artifact_id,
                        artifact.operation_id,
                        artifact.file_name,
                        artifact.media_type,
                        artifact.size_bytes,
                        artifact.content_digest,
                        artifact.storage_path,
                        artifact.created_at,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"Artifact {artifact.artifact_id} already exists") from error
        return artifact

    def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        row = self._connection.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(f"Artifact {artifact_id} was not found")
        return ArtifactRecord(
            artifact_id=cast(str, row["artifact_id"]),
            operation_id=cast(str, row["operation_id"]),
            file_name=cast(str, row["file_name"]),
            media_type=cast(str, row["media_type"]),
            size_bytes=cast(int, row["size_bytes"]),
            content_digest=cast(str, row["content_digest"]),
            storage_path=cast(str, row["storage_path"]),
            created_at=cast(str, row["created_at"]),
        )

    def _active_version(self, server_id: str) -> str | None:
        row = self._connection.execute(
            "SELECT catalog_version FROM active_catalogs WHERE server_id = ?", (server_id,)
        ).fetchone()
        return cast(str, row["catalog_version"]) if row is not None else None


def _server_from_row(row: sqlite3.Row) -> ServerRegistration:
    return ServerRegistration(
        server_id=cast(str, row["server_id"]),
        display_name=cast(str, row["display_name"]),
        endpoint=cast(str, row["endpoint"]),
        auth_profile=cast(str, row["auth_profile"]),
        provider_pack=cast(str | None, row["provider_pack"]),
        enabled=bool(row["enabled"]),
        created_at=cast(str, row["created_at"]),
        updated_at=cast(str, row["updated_at"]),
    )


def _snapshot_from_row(row: sqlite3.Row) -> DiscoverySnapshot:
    server_info = _load_mapping(cast(str, row["server_info_json"]))
    capabilities = _load_mapping(cast(str, row["capabilities_json"]))
    primitives = _load_sequence(cast(str, row["primitives_json"]))
    return DiscoverySnapshot(
        snapshot_id=cast(int, row["snapshot_id"]),
        server_id=cast(str, row["server_id"]),
        protocol_version=cast(str, row["protocol_version"]),
        discovered_at=cast(str, row["discovered_at"]),
        snapshot_digest=cast(str, row["snapshot_digest"]),
        server_info=cast(Mapping[str, object], freeze_json(server_info)),
        capabilities=cast(Mapping[str, object], freeze_json(capabilities)),
        primitives=tuple(
            cast(Mapping[str, object], freeze_json(item)) for item in primitives
        ),
    )


def _operation_from_row(row: sqlite3.Row) -> OperationRecord:
    result_value = cast(str | None, row["result_json"])
    return OperationRecord(
        operation_id=cast(str, row["operation_id"]),
        request_id=cast(str, row["request_id"]),
        server_id=cast(str, row["server_id"]),
        catalog_version=cast(str, row["catalog_version"]),
        public_operation_id=cast(str, row["public_operation_id"]),
        local_identity_id=cast(str, row["local_identity_id"]),
        connection_id=cast(str, row["connection_id"]),
        classification=cast(str, row["classification"]),
        status=cast(str, row["status"]),
        idempotency_key=cast(str | None, row["idempotency_key"]),
        request_digest=cast(str, row["request_digest"]),
        approval_id=cast(str | None, row["approval_id"]),
        created_at=cast(str, row["created_at"]),
        updated_at=cast(str, row["updated_at"]),
        result=(
            cast(Mapping[str, object], freeze_json(_load_mapping(result_value)))
            if result_value is not None
            else None
        ),
        error_code=cast(str | None, row["error_code"]),
        error_message=cast(str | None, row["error_message"]),
        retryable=bool(row["retryable"]),
    )


def _json(value: object) -> str:
    return json.dumps(thaw_json(value), separators=(",", ":"))


def _load_mapping(value: str) -> Mapping[str, object]:
    loaded: object = json.loads(value)
    if not isinstance(loaded, dict):
        raise InvalidStateError("Stored JSON object is invalid")
    mapping = cast(dict[object, object], loaded)
    if not all(isinstance(key, str) for key in mapping):
        raise InvalidStateError("Stored JSON object is invalid")
    return cast(Mapping[str, object], mapping)


def _load_sequence(value: str) -> tuple[Mapping[str, object], ...]:
    loaded: object = json.loads(value)
    if not isinstance(loaded, list):
        raise InvalidStateError("Stored JSON array is invalid")
    result: list[Mapping[str, object]] = []
    for item in cast(Sequence[object], loaded):
        if not isinstance(item, dict):
            raise InvalidStateError("Stored primitive is invalid")
        mapping = cast(dict[object, object], item)
        if not all(isinstance(key, str) for key in mapping):
            raise InvalidStateError("Stored primitive is invalid")
        result.append(cast(Mapping[str, object], mapping))
    return tuple(result)


for _name, _member in list(vars(SqliteControlPlaneRepository).items()):
    if callable(_member) and not _name.startswith("_") and _name != "close":
        setattr(SqliteControlPlaneRepository, _name, _locked(_member))
