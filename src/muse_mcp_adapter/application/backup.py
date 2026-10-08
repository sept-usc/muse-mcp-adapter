"""Backing up and restoring everything the adapter keeps.

A backup is one folder holding a consistent copy of the database (taken with SQLite's online backup,
so the service can keep running), the key file and the encrypted token store that belong to it, and
the stored artifacts, plus a manifest of hashes. The folder contains secrets, so it is created
readable by its owner only. Restore checks every hash before it replaces anything and moves what it
replaces aside instead of deleting it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from ..domain.errors import InvalidStateError

MANIFEST = "MANIFEST.json"
PREFIX = "adapter-"
_DATABASE = "adapter.db"
_KEY = "adapter.key"
_OAUTH = "adapter.oauth"
_ARTIFACTS = "artifacts"
_FORMAT = 1


@dataclass(frozen=True, slots=True)
class BackupInfo:
    path: Path
    files: int
    bytes: int


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _private_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as out, source.open("rb") as handle:
        shutil.copyfileobj(handle, out)


def create_backup(
    database: Path,
    destination: Path,
    *,
    artifact_root: Path | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> BackupInfo:
    if not database.is_file():
        raise InvalidStateError(f"There is no database at {database}")
    moment = now()
    target = destination / f"{PREFIX}{_stamp(moment)}"
    if target.exists():
        raise InvalidStateError(f"A backup named {target.name} already exists")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.mkdir(mode=0o700)
    try:
        source = sqlite3.connect(database)
        copy = sqlite3.connect(target / _DATABASE)
        try:
            source.backup(copy)
        finally:
            copy.close()
            source.close()
        os.chmod(target / _DATABASE, 0o600)
        for suffix, name in ((".key", _KEY), (".oauth", _OAUTH)):
            companion = database.with_suffix(suffix)
            if companion.is_file():
                _private_copy(companion, target / name)
        artifacts = artifact_root or database.parent / _ARTIFACTS
        if artifacts.is_dir():
            for item in sorted(artifacts.rglob("*")):
                if item.is_file() and not item.is_symlink():
                    _private_copy(item, target / _ARTIFACTS / item.relative_to(artifacts))
        files = {
            str(item.relative_to(target)): {"sha256": _sha256(item), "bytes": item.stat().st_size}
            for item in sorted(target.rglob("*"))
            if item.is_file()
        }
        manifest = {"format": _FORMAT, "createdAt": moment.isoformat(), "files": files}
        manifest_path = target / MANIFEST
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        os.chmod(manifest_path, 0o600)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return BackupInfo(target, len(files), sum(int(item["bytes"]) for item in files.values()))


def list_backups(destination: Path) -> list[Path]:
    if not destination.is_dir():
        return []
    return sorted(
        item
        for item in destination.iterdir()
        if item.is_dir() and item.name.startswith(PREFIX) and (item / MANIFEST).is_file()
    )


def prune(destination: Path, keep: int) -> list[Path]:
    """Remove the oldest backups so that ``keep`` remain. Other files are never touched."""
    if keep < 1:
        raise InvalidStateError("keep must be at least 1")
    found = list_backups(destination)
    removed = found[: max(0, len(found) - keep)]
    for item in removed:
        shutil.rmtree(item)
    return removed


def verify_backup(source: Path) -> dict[str, dict[str, Any]]:
    """Check a backup folder against its manifest and return the manifest's file list."""
    manifest_path = source / MANIFEST
    if not manifest_path.is_file():
        raise InvalidStateError(f"{source} is not a backup (no {MANIFEST})")
    manifest = cast(dict[str, Any], json.loads(manifest_path.read_text()))
    files = manifest.get("files")
    if manifest.get("format") != _FORMAT or not isinstance(files, dict) or _DATABASE not in files:
        raise InvalidStateError("The backup manifest is not one this version can read")
    listed = cast(dict[str, dict[str, Any]], files)
    root = source.resolve()
    for name, info in listed.items():
        path = (source / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise InvalidStateError(f"The backup lists a file that is not inside it: {name}")
        if _sha256(path) != info.get("sha256"):
            raise InvalidStateError(f"The backup is damaged: {name} does not match its hash")
    check = sqlite3.connect(f"file:{source / _DATABASE}?mode=ro", uri=True)
    try:
        outcome = check.execute("PRAGMA integrity_check").fetchone()
    finally:
        check.close()
    if not outcome or outcome[0] != "ok":
        raise InvalidStateError("The backed-up database fails its integrity check")
    return listed


def restore_backup(
    source: Path,
    database: Path,
    *,
    artifact_root: Path | None = None,
    force: bool = False,
    now: Callable[[], datetime] = _utc_now,
) -> dict[str, object]:
    """Put a backup back. The service must be stopped. Existing data is moved aside, not deleted."""
    files = verify_backup(source)
    existing = [
        path
        for path in (database, database.with_suffix(".key"), database.with_suffix(".oauth"))
        if path.exists()
    ]
    if existing and not force:
        raise InvalidStateError(
            f"{database} already holds data. Pass --force to move it aside and restore over it."
        )
    artifacts = artifact_root or database.parent / _ARTIFACTS
    aside: Path | None = None
    if existing:
        aside = database.parent / f"pre-restore-{_stamp(now())}"
        aside.mkdir(mode=0o700, parents=True)
        for path in existing + [Path(f"{database}-wal"), Path(f"{database}-shm")]:
            if path.exists():
                shutil.move(str(path), aside / path.name)
        if artifacts.is_dir():
            shutil.move(str(artifacts), aside / _ARTIFACTS)
    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _place(source / _DATABASE, database)
    if _KEY in files:
        _place(source / _KEY, database.with_suffix(".key"))
    if _OAUTH in files:
        _place(source / _OAUTH, database.with_suffix(".oauth"))
    for name in files:
        if name.startswith(f"{_ARTIFACTS}/"):
            _place(source / name, artifacts / Path(name).relative_to(_ARTIFACTS))
    return {
        "restoredFrom": str(source),
        "database": str(database),
        "movedAside": str(aside) if aside else None,
        "files": len(files),
    }


def _place(source: Path, target: Path) -> None:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.restoring")
    shutil.copyfile(source, temporary)
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
