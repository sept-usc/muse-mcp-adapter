from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Protocol, cast

from ..catalog.canonical import digest, freeze_json
from ..domain.errors import InvalidStateError
from ..domain.models import DiscoverySnapshot, ServerRegistration


class DiscoveryClient(Protocol):
    def discover(self, server: ServerRegistration) -> DiscoverySnapshot: ...


class ManifestDiscoveryClient:
    """Deterministic Phase 2 client backed by a validated discovery manifest."""

    def __init__(self, manifest: Mapping[str, object], discovered_at: str | None = None) -> None:
        self._manifest = manifest
        self._discovered_at = discovered_at

    def discover(self, server: ServerRegistration) -> DiscoverySnapshot:
        protocol_version = self._manifest.get("protocolVersion")
        if not isinstance(protocol_version, str) or not protocol_version:
            raise InvalidStateError("Discovery manifest requires protocolVersion")

        server_info = _mapping(self._manifest.get("serverInfo"), "serverInfo")
        capabilities = _mapping(self._manifest.get("capabilities"), "capabilities")
        raw_primitives = self._manifest.get("primitives")
        if not _sequence(raw_primitives):
            raise InvalidStateError("Discovery manifest requires a primitives array")

        primitives = tuple(
            cast(
                Mapping[str, object],
                freeze_json(_mapping(item, "primitive")),
            )
            for item in cast(Sequence[object], raw_primitives)
        )
        discovered_at = self._discovered_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
        digest_payload = {
            "serverId": server.server_id,
            "protocolVersion": protocol_version,
            "serverInfo": server_info,
            "capabilities": capabilities,
            "primitives": primitives,
        }

        return DiscoverySnapshot(
            snapshot_id=None,
            server_id=server.server_id,
            protocol_version=protocol_version,
            discovered_at=discovered_at,
            snapshot_digest=digest(digest_payload),
            server_info=cast(Mapping[str, object], freeze_json(server_info)),
            capabilities=cast(Mapping[str, object], freeze_json(capabilities)),
            primitives=primitives,
        )


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidStateError(f"Discovery manifest requires object field {field}")
    mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise InvalidStateError(f"Discovery manifest field {field} has a non-string key")
    return cast(Mapping[str, object], mapping)


def _sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
