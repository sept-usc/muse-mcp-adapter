"""Backup and restore of the adapter's state."""

import json
import os
import sqlite3
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from muse_mcp_adapter.application.backup import (
    MANIFEST,
    create_backup,
    list_backups,
    prune,
    restore_backup,
    verify_backup,
)
from muse_mcp_adapter.application.control_plane_service import ControlPlaneService
from muse_mcp_adapter.cli import main
from muse_mcp_adapter.connections import ConnectionService
from muse_mcp_adapter.domain.errors import InvalidStateError
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from muse_mcp_adapter.transport.security import EndpointSecurityPolicy


def _state(directory: Path) -> tuple[Path, str]:
    """A database with a server and a connection, plus key file, token store and an artifact."""
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "adapter.db"
    repository = SqliteControlPlaneRepository(database)
    repository.initialize()
    ControlPlaneService(
        repository, endpoint_policy=EndpointSecurityPolicy.local_development()
    ).register_server(
        server_id="tracker",
        display_name="Tracker",
        endpoint="http://127.0.0.1:9101/mcp",
        auth_profile="none",
    )
    token = (
        ConnectionService(repository)
        .link(server_id="tracker", label="Muse", credential_kind="none")
        .token
    )
    repository.close()
    (directory / "adapter.key").write_bytes(b"the-key")
    (directory / "adapter.oauth").write_bytes(b"encrypted-tokens")
    (directory / "artifacts").mkdir()
    (directory / "artifacts" / "result.bin").write_bytes(b"stored result")
    return database, token


def _at(hour: int):  # type: ignore[no-untyped-def]
    return lambda: datetime(2026, 10, 8, hour, 0, 0, tzinfo=UTC)


def test_a_backup_holds_everything_and_is_private(tmp_path: Path) -> None:
    database, _ = _state(tmp_path / "live")
    info = create_backup(database, tmp_path / "backups", now=_at(1))

    assert info.path.name == "adapter-20261008T010000Z"
    names = {p.name for p in info.path.rglob("*") if p.is_file()}
    assert names == {"adapter.db", "adapter.key", "adapter.oauth", "result.bin", MANIFEST}
    assert stat.S_IMODE(info.path.stat().st_mode) == 0o700
    for item in info.path.rglob("*"):
        if item.is_file():
            assert stat.S_IMODE(item.stat().st_mode) == 0o600
    verify_backup(info.path)


def test_a_backup_taken_while_the_service_writes_is_consistent(tmp_path: Path) -> None:
    database, _ = _state(tmp_path / "live")
    stop = threading.Event()
    written = [0]

    def write() -> None:
        connection = sqlite3.connect(database, timeout=5)
        while not stop.is_set():
            connection.execute(
                "INSERT INTO admin_audit (occurred_at, actor, action, server_id, data_json) "
                "VALUES (?, 'x', 'noise', 'tracker', '{}')",
                ("2026-10-08T00:00:00Z",),
            )
            connection.commit()
            written[0] += 1
        connection.close()

    writer = threading.Thread(target=write)
    writer.start()
    try:
        infos = [create_backup(database, tmp_path / "backups", now=_at(h)) for h in (1, 2, 3)]
    finally:
        stop.set()
        writer.join()
    assert written[0] > 10  # the writer really was busy while the backups were taken
    for info in infos:
        verify_backup(info.path)  # integrity check passes on every copy


def test_restore_brings_back_a_working_connection(tmp_path: Path) -> None:
    database, token = _state(tmp_path / "live")
    info = create_backup(database, tmp_path / "backups", now=_at(1))

    fresh = tmp_path / "new-vm" / "adapter.db"
    result = restore_backup(info.path, fresh)
    assert result["movedAside"] is None

    repository = SqliteControlPlaneRepository(fresh)
    repository.initialize()
    assert ConnectionService(repository).authenticate(token).label == "Muse"  # the same token
    repository.close()
    assert (fresh.parent / "adapter.key").read_bytes() == b"the-key"
    assert (fresh.parent / "adapter.oauth").read_bytes() == b"encrypted-tokens"
    assert (fresh.parent / "artifacts" / "result.bin").read_bytes() == b"stored result"


def test_restore_refuses_to_overwrite_and_moves_data_aside_when_forced(tmp_path: Path) -> None:
    database, _ = _state(tmp_path / "live")
    info = create_backup(database, tmp_path / "backups", now=_at(1))
    other, _ = _state(tmp_path / "other")

    with pytest.raises(InvalidStateError, match="--force"):
        restore_backup(info.path, other)
    assert other.is_file()  # untouched

    result = restore_backup(info.path, other, force=True, now=_at(2))
    aside = Path(str(result["movedAside"]))
    assert aside.name == "pre-restore-20261008T020000Z"
    assert (aside / "adapter.db").is_file() and (aside / "adapter.key").is_file()


def test_a_damaged_backup_is_refused_before_anything_is_replaced(tmp_path: Path) -> None:
    database, _ = _state(tmp_path / "live")
    info = create_backup(database, tmp_path / "backups", now=_at(1))
    with (info.path / "adapter.key").open("r+b") as handle:
        handle.write(b"X")  # one changed byte

    target = tmp_path / "target" / "adapter.db"
    with pytest.raises(InvalidStateError, match="damaged"):
        restore_backup(info.path, target)
    assert not target.exists()


def test_a_manifest_that_points_outside_the_backup_is_refused(tmp_path: Path) -> None:
    database, _ = _state(tmp_path / "live")
    info = create_backup(database, tmp_path / "backups", now=_at(1))
    secret = tmp_path / "outside.txt"
    secret.write_text("not part of the backup")
    manifest = json.loads((info.path / MANIFEST).read_text())
    manifest["files"]["../../outside.txt"] = {"sha256": "0" * 64, "bytes": 1}
    (info.path / MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(InvalidStateError, match="not inside"):
        verify_backup(info.path)


def test_prune_keeps_the_newest_and_leaves_other_files_alone(tmp_path: Path) -> None:
    database, _ = _state(tmp_path / "live")
    destination = tmp_path / "backups"
    for hour in (1, 2, 3, 4):
        create_backup(database, destination, now=_at(hour))
    (destination / "notes.txt").write_text("keep me")
    (destination / "adapter-unrelated").mkdir()

    removed = prune(destination, keep=2)
    assert [p.name for p in removed] == ["adapter-20261008T010000Z", "adapter-20261008T020000Z"]
    assert [p.name for p in list_backups(destination)] == [
        "adapter-20261008T030000Z",
        "adapter-20261008T040000Z",
    ]
    assert (destination / "notes.txt").exists() and (destination / "adapter-unrelated").exists()
    with pytest.raises(InvalidStateError):
        prune(destination, keep=0)


def test_the_command_line_backs_up_and_restores(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database, token = _state(tmp_path / "live")
    assert (
        main(["--database", str(database), "backup", "--to", str(tmp_path / "b"), "--keep", "3"])
        == 0
    )
    made = json.loads(capsys.readouterr().out)
    assert made["files"] == 4 and made["removedOld"] == []

    target = tmp_path / "restored" / "adapter.db"
    assert main(["--database", str(target), "restore", "--from", made["backup"]]) == 0
    assert json.loads(capsys.readouterr().out)["database"] == str(target)
    repository = SqliteControlPlaneRepository(target)
    repository.initialize()
    assert ConnectionService(repository).authenticate(token).server_id == "tracker"
    repository.close()
    assert os.path.exists(target.with_suffix(".key"))


def test_backing_up_a_missing_database_says_so(tmp_path: Path) -> None:
    with pytest.raises(InvalidStateError, match="no database"):
        create_backup(tmp_path / "nope.db", tmp_path / "b")
