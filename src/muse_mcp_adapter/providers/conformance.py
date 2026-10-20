from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from ..domain.models import DiscoverySnapshot
from .packs import ProviderPack


@dataclass(frozen=True, slots=True)
class ProviderConformanceResult:
    pack_id: str
    passed: bool
    issues: tuple[str, ...]


class ProviderConformanceRunner:
    """Applies one generic discovery contract to every provider pack."""

    def evaluate(
        self,
        pack: ProviderPack,
        snapshot: DiscoverySnapshot,
        *,
        auth_mode: str,
    ) -> ProviderConformanceResult:
        issues: list[str] = []
        if auth_mode not in pack.auth_modes:
            issues.append(f"Unsupported authentication mode {auth_mode}")
        discovered_types = {
            item_type
            for primitive in snapshot.primitives
            if isinstance((item_type := primitive.get("type")), str)
        }
        raw_required = pack.conformance.get("requiredPrimitiveTypes", ())
        required = (
            tuple(item for item in cast(Sequence[object], raw_required) if isinstance(item, str))
            if isinstance(raw_required, Sequence)
            and not isinstance(raw_required, (str, bytes, bytearray))
            else ()
        )
        missing = set(required) - discovered_types
        if missing:
            issues.append("Missing primitive types: " + ", ".join(sorted(missing)))
        if not snapshot.protocol_version:
            issues.append("Discovery did not negotiate an MCP protocol version")
        if not snapshot.snapshot_digest.startswith("sha256:"):
            issues.append("Discovery snapshot is not content-addressed")
        return ProviderConformanceResult(
            pack_id=pack.pack_id,
            passed=not issues,
            issues=tuple(issues),
        )
