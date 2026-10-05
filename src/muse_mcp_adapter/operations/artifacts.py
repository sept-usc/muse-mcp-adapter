from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from ..domain.errors import DownstreamContentError, EntityNotFoundError
from ..domain.models import ArtifactRecord
from ..persistence.sqlite_repository import SqliteControlPlaneRepository

_SAFE_FILE_NAME = re.compile(r"[^A-Za-z0-9._-]")


class FileArtifactStore:
    """Bounded, operation-scoped storage with durable metadata and content digests."""

    def __init__(
        self,
        root: Path,
        repository: SqliteControlPlaneRepository,
        *,
        clock: Callable[[], str],
        max_artifact_bytes: int = 10 * 1024 * 1024,
        artifact_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._root = root.resolve()
        self._repository = repository
        self._clock = clock
        self._max_bytes = max_artifact_bytes
        self._artifact_id_factory = artifact_id_factory or (lambda: str(uuid4()))

    def put(
        self,
        *,
        operation_id: str,
        file_name: str,
        media_type: str,
        content: bytes,
    ) -> ArtifactRecord:
        if not content or len(content) > self._max_bytes:
            raise DownstreamContentError("Artifact is empty or exceeds the size limit")
        safe_name = _SAFE_FILE_NAME.sub("_", Path(file_name).name).strip(".")
        if not safe_name:
            raise DownstreamContentError("Artifact file name is invalid")
        artifact_id = self._artifact_id_factory()
        directory = (self._root / operation_id).resolve()
        if self._root not in directory.parents:
            raise DownstreamContentError("Artifact operation path escapes the storage root")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = (directory / f"{artifact_id}-{safe_name}").resolve()
        if directory not in destination.parents:
            raise DownstreamContentError("Artifact path escapes its operation directory")
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
        os.replace(temporary, destination)
        record = ArtifactRecord(
            artifact_id=artifact_id,
            operation_id=operation_id,
            file_name=safe_name,
            media_type=media_type,
            size_bytes=len(content),
            content_digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
            storage_path=str(destination.relative_to(self._root)),
            created_at=self._clock(),
        )
        return self._repository.save_artifact(record)

    def read(self, artifact_id: str, *, operation_id: str) -> bytes:
        """Read an artifact; the caller must name the operation that owns it."""
        record = self._repository.get_artifact(artifact_id)
        if record.operation_id != operation_id:
            raise EntityNotFoundError(f"Artifact {artifact_id} was not found")
        path = (self._root / record.storage_path).resolve()
        if self._root not in path.parents:
            raise DownstreamContentError("Stored artifact path escapes the storage root")
        content = path.read_bytes()
        actual = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if actual != record.content_digest:
            raise DownstreamContentError("Stored artifact digest does not match metadata")
        return content

    def metadata(self, artifact_id: str) -> ArtifactRecord:
        return self._repository.get_artifact(artifact_id)
