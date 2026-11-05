"""Whether a change to what a connection exposes gives anyone more than it did before.

Making access smaller is the one change an agent may make on its own. The question "is this
smaller?" is answered here, away from any one caller, so the control plane can ask it at the moment
a catalog is activated and not only where a caller remembered to.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from ..catalog.canonical import thaw_json
from ..domain.models import PublishedCatalog
from . import expiry
from .argument_limits import no_looser

CLASS_RANK: Mapping[str, int] = {"read": 0, "write": 1, "sensitive_write": 2}
_IGNORED_WHEN_COMPARING = frozenset(
    {"classification", "timeoutSeconds", "maxResultBytes", "argumentPatterns", "expiresAt"}
)
_LIMITS = ("timeoutSeconds", "maxResultBytes")


@dataclass(frozen=True, slots=True)
class Exposure:
    """One exposed operation as it is published: which definition, under which policy."""

    definition_digest: str
    policy: Mapping[str, object]


def exposures_of(catalog: PublishedCatalog | None) -> dict[str, Exposure]:
    if catalog is None:
        return {}
    return {
        item.primitive_key: Exposure(
            item.definition_digest, cast(Mapping[str, object], thaw_json(item.policy))
        )
        for item in catalog.primitives
    }


def classify_change(old: Mapping[str, Exposure], new: Mapping[str, Exposure]) -> str:
    """``none``, ``narrowing`` or ``widening``.

    A change is narrowing only when it exposes nothing that was not already exposed, keeps every
    definition and every policy no looser than before, and drops or tightens something. Anything
    else, including a retained operation whose definition changed, is widening and needs the
    owner.
    """
    if old == new:
        return "none"
    if not new.keys() <= old.keys():
        return "widening"
    for key, exposure in new.items():
        before = old[key]
        if exposure.definition_digest != before.definition_digest:
            return "widening"
        if not _no_looser(before.policy, exposure.policy):
            return "widening"
    return "narrowing"


def _no_looser(old: Mapping[str, object], new: Mapping[str, object]) -> bool:
    if old == new:
        return True
    old_rank = CLASS_RANK.get(str(old.get("classification")))
    new_rank = CLASS_RANK.get(str(new.get("classification")))
    if old_rank is None or new_rank is None or new_rank < old_rank:
        return False
    for limit in _LIMITS:
        before, after = old.get(limit), new.get(limit)
        if isinstance(before, int) and not isinstance(before, bool):
            if not isinstance(after, int) or isinstance(after, bool) or after > before:
                return False
        elif before is not None or (after is not None and not isinstance(after, int)):
            return False
    return (
        expiry.no_later(old.get("expiresAt"), new.get("expiresAt"))
        and _limits_no_looser(old, new)
        and _without(old) == _without(new)
    )


def _limits_no_looser(old: Mapping[str, object], new: Mapping[str, object]) -> bool:
    """Every limit there was is still there, unchanged; new ones only tighten."""
    before, after = old.get("argumentPatterns"), new.get("argumentPatterns")
    if before is None:
        return True
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return False
    kept = cast(Mapping[object, object], after)
    return all(
        isinstance(pattern, str)
        and isinstance(kept.get(name), str)
        and no_looser(pattern, cast(str, kept[name]))
        for name, pattern in cast(Mapping[object, object], before).items()
    )


def _without(policy: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in policy.items()
        if key not in _IGNORED_WHEN_COMPARING and key != "readOnlyReviewed"
    }
