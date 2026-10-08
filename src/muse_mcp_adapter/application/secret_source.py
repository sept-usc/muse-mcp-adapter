"""Where the adapter finds secrets: the environment, then files in a secrets directory.

A container platform (Docker or Compose secrets, systemd credentials, Kubernetes secrets) can
hand a secret to a process as a file. Reading it from there keeps it out of the process
environment, where ``docker inspect`` and ``/proc/<pid>/environ`` would show it, and lets a secret
be added or changed without restarting the service, because it is read when it is needed.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from pathlib import Path

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
MAX_SECRET_BYTES = 64 * 1024


class SecretSource(Mapping[str, str]):
    """A read-only view of the environment that falls back to ``<directory>/<NAME>``."""

    def __init__(self, environ: Mapping[str, str], directory: Path | None = None) -> None:
        self._environ = environ
        self._directory = directory

    def __getitem__(self, name: str) -> str:
        value = self._environ.get(name)
        if value:
            return value
        from_file = self._read(name)
        if from_file is None:
            raise KeyError(name)
        return from_file

    def __iter__(self) -> Iterator[str]:
        seen = set(self._environ)
        yield from self._environ
        for name in self._names():
            if name not in seen:
                yield name

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def _names(self) -> list[str]:
        directory = self._directory
        if directory is None or not directory.is_dir():
            return []
        return sorted(
            item.name for item in directory.iterdir() if _NAME.match(item.name) and item.is_file()
        )

    def _read(self, name: str) -> str | None:
        directory = self._directory
        if directory is None or not _NAME.match(name):
            return None
        path = directory / name
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(directory.resolve()) or not resolved.is_file():
                return None  # a link that leaves the directory is never followed
            if resolved.stat().st_size > MAX_SECRET_BYTES:
                return None
            text = resolved.read_text(encoding="utf-8").rstrip("\r\n")
        except (OSError, UnicodeDecodeError):
            return None
        return text or None
