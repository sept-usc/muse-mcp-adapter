"""Turn a request and what a server offers into the policy the owner reviews.

Everything here is a pure function of its inputs: the same request, the same discovery and the
same live catalog give the same policy and the same digest, so a change can be replayed and
checked later. Nothing is exposed unless the request names it or asks for a class that expands to
it, and what the owner finally approves can only be chosen from what the server offers.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from ..application.policy_template import draft_policies_for, name_suggests_read
from ..catalog.canonical import digest, thaw_json
from ..domain.constants import primitive_key
from ..domain.errors import InvalidStateError, RequestValidationError
from ..domain.models import PublishedCatalog
from .models import SetupRequest

VERSION = 1
MAX_OFFERED = 2000  # a page of more than this is not a review anyone can do
CLASS_RANK: Mapping[str, int] = {"read": 0, "write": 1, "sensitive_write": 2}
_KIND_ORDER = {"tool": 0, "resource": 1, "resource_template": 2, "prompt": 3}
_IGNORED_WHEN_COMPARING = frozenset({"classification", "timeoutSeconds", "maxResultBytes"})
_LIMITS = ("timeoutSeconds", "maxResultBytes")


@dataclass(frozen=True, slots=True)
class Candidate:
    """One thing the server offers, as the owner sees it on the review page."""

    key: str
    name: str
    kind: str
    classification: str
    evidence: str
    description: str  # the server's own words; shown only on the review page, marked as such
    public_operation_id: str
    requested: bool
    selected: bool
    suggested: bool  # looks read-only by its name, but the server did not say so
    reviewable: bool  # a change by default; the owner may mark it read-only after checking
    changed: bool  # the server changed it since it was published

    def to_json(self) -> dict[str, object]:
        return {
            "key": self.key,
            "name": self.name,
            "kind": self.kind,
            "classification": self.classification,
            "evidence": self.evidence,
            "description": self.description,
            "publicOperationId": self.public_operation_id,
            "requested": self.requested,
            "selected": self.selected,
            "suggested": self.suggested,
            "reviewable": self.reviewable,
            "changed": self.changed,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> Candidate:
        return cls(
            key=_str(value, "key"),
            name=_str(value, "name"),
            kind=_str(value, "kind"),
            classification=_str(value, "classification"),
            evidence=_str(value, "evidence"),
            description=_str(value, "description"),
            public_operation_id=_str(value, "publicOperationId"),
            requested=value.get("requested") is True,
            selected=value.get("selected") is True,
            suggested=value.get("suggested") is True,
            reviewable=value.get("reviewable") is True,
            changed=value.get("changed") is True,
        )


@dataclass(frozen=True, slots=True)
class Computed:
    """The compiled proposal: every offered primitive with its drafted policy, and the choice."""

    candidates: tuple[Candidate, ...]
    policies: Mapping[str, Mapping[str, object]]  # one entry per offered primitive
    unresolved: tuple[str, ...]  # names asked for that could not be matched
    gone: tuple[str, ...]  # exposed now but no longer offered by the server
    unenforced: tuple[str, ...]  # limits asked for in words that nothing can enforce
    base_version: str | None  # the live catalog this was compiled against
    digest: str

    @property
    def selected(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.candidates if item.selected)

    @property
    def requested(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.candidates if item.requested)

    def to_json(self) -> dict[str, object]:
        return {
            "candidates": [item.to_json() for item in self.candidates],
            "policies": {key: dict(entry) for key, entry in self.policies.items()},
            "unresolved": list(self.unresolved),
            "gone": list(self.gone),
            "unenforced": list(self.unenforced),
            "baseVersion": self.base_version,
            "digest": self.digest,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> Computed:
        candidates = tuple(
            Candidate.from_json(_mapping(item)) for item in _sequence(value.get("candidates"))
        )
        raw_policies = _mapping(value.get("policies"))
        base = value.get("baseVersion")
        return cls(
            candidates=candidates,
            policies={key: _mapping(entry) for key, entry in raw_policies.items()},
            unresolved=tuple(str(item) for item in _sequence(value.get("unresolved"))),
            gone=tuple(str(item) for item in _sequence(value.get("gone"))),
            unenforced=tuple(str(item) for item in _sequence(value.get("unenforced"))),
            base_version=base if isinstance(base, str) else None,
            digest=_str(value, "digest"),
        )


@dataclass(frozen=True, slots=True)
class Final:
    """What the owner approved: the full policy list, and how it differs from the proposal."""

    policies: tuple[Mapping[str, object], ...]
    selected: tuple[str, ...]
    added_by_owner: tuple[str, ...]
    removed_by_owner: tuple[str, ...]
    marked_read: tuple[str, ...]
    exposed_not_requested: tuple[str, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class Exposure:
    """One exposed operation as it is published: which definition, under which policy."""

    definition_digest: str
    policy: Mapping[str, object]


def compile_proposal(
    *,
    primitives: Sequence[Mapping[str, object]],
    request: SetupRequest,
    current: Mapping[str, Mapping[str, object]] | None,
    current_digests: Mapping[str, str] | None,
    snapshot_digest: str,
    base_version: str | None,
) -> Computed:
    """Draft the policy for everything offered and choose only what the request asks for.

    ``current`` holds the policies that are live now (for a change to an existing connection) and
    ``current_digests`` the digest of each live definition; both are empty for a new connection.
    """
    if len(primitives) > MAX_OFFERED:
        raise RequestValidationError(
            f"The server offers {len(primitives)} things, which is too many to review. "
            "Connect it at the command line instead."
        )
    live = current or {}
    live_digests = current_digests or {}
    index: dict[str, Mapping[str, object]] = {}
    for primitive in primitives:
        index[primitive_key(primitive)] = primitive

    reserved = {str(entry.get("publicOperationId")) for key, entry in live.items() if key in index}
    drafts = {
        str(entry["primitiveKey"]): entry
        for entry in draft_policies_for(primitives, reserved_ids=reserved)
    }

    requested: list[str] = []
    if request.read_only:
        requested.extend(key for key, entry in drafts.items() if entry["classification"] == "read")
    named, missing_allow = _resolve(request.also_allow, index)
    requested.extend(named)
    removed, missing_exclude = _resolve(request.exclude, index)
    removed_set = set(removed)
    requested_keys = [key for key in dict.fromkeys(requested) if key not in removed_set]

    kept = {key for key in live if key in index}
    chosen = (kept | set(requested_keys)) - removed_set

    changed_keys = {
        key for key in kept if key in live_digests and live_digests[key] != digest(index[key])
    }
    policies: dict[str, Mapping[str, object]] = {}
    for key, draft in drafts.items():
        if key in live and key not in changed_keys:
            entry = dict(live[key])
        else:
            entry = dict(draft)
            if key in live:  # the server changed it: draft afresh, keep the name Muse knows
                entry["publicOperationId"] = live[key].get("publicOperationId")
        entry["allowed"] = key in chosen
        if key in chosen and index[key].get("type") == "tool":
            if request.timeout_seconds is not None:
                entry["timeoutSeconds"] = request.timeout_seconds
            if request.max_result_bytes is not None:
                entry["maxResultBytes"] = request.max_result_bytes
        policies[key] = entry

    candidates = tuple(
        _candidate(
            key,
            index[key],
            policies[key],
            requested=key in requested_keys,
            selected=key in chosen,
            changed=key in changed_keys,
        )
        for key in sorted(index, key=lambda item: _order(index[item]))
    )
    unenforced = (f"Not enforced: {request.notes}",) if request.notes else ()
    payload = {
        "v": VERSION,
        "snapshot": snapshot_digest,
        "base": base_version,
        "request": request.to_json(),
        "selected": {key: policies[key] for key in sorted(chosen)},
    }
    return Computed(
        candidates=candidates,
        policies=policies,
        unresolved=tuple(missing_allow + missing_exclude),
        gone=tuple(sorted(key for key in live if key not in index)),
        unenforced=unenforced,
        base_version=base_version,
        digest=digest(payload),
    )


def finalize(computed: Computed, chosen: Collection[str], reviewed_read: Collection[str]) -> Final:
    """Apply the owner's choices to a compiled proposal.

    The owner may remove anything, add anything the server offers, and mark a change-by-default
    tool read-only after checking it. Nothing outside what the server offers can be chosen.
    """
    known = {item.key: item for item in computed.candidates}
    chosen_set = set(chosen)
    unknown = sorted(chosen_set - known.keys())
    if unknown:
        raise RequestValidationError("The server does not offer: " + ", ".join(unknown))
    marked = sorted(
        key for key in set(reviewed_read) & chosen_set if key in known and known[key].reviewable
    )
    policies: list[Mapping[str, object]] = []
    for key in sorted(known):
        entry = dict(computed.policies[key])
        entry["allowed"] = key in chosen_set
        if key in marked:
            entry["classification"] = "read"
            entry["readOnlyReviewed"] = True
        policies.append(entry)
    proposed = set(computed.selected)
    requested = set(computed.requested)
    return Final(
        policies=tuple(policies),
        selected=tuple(sorted(chosen_set)),
        added_by_owner=tuple(sorted(chosen_set - proposed)),
        removed_by_owner=tuple(sorted(proposed - chosen_set)),
        marked_read=tuple(marked),
        exposed_not_requested=tuple(sorted(chosen_set - requested)),
        digest=digest({"proposal": computed.digest, "chosen": sorted(chosen_set), "read": marked}),
    )


def live_policies(catalog: PublishedCatalog) -> tuple[dict[str, dict[str, object]], dict[str, str]]:
    """The policies and definition digests of what a catalog exposes, keyed by primitive."""
    policies: dict[str, dict[str, object]] = {}
    digests: dict[str, str] = {}
    for item in catalog.primitives:
        policies[item.primitive_key] = cast(dict[str, object], thaw_json(item.policy))
        digests[item.primitive_key] = item.definition_digest
    return policies, digests


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
    return _without(old) == _without(new)


def _without(policy: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in policy.items()
        if key not in _IGNORED_WHEN_COMPARING and key != "readOnlyReviewed"
    }


def _resolve(
    names: Sequence[str], index: Mapping[str, Mapping[str, object]]
) -> tuple[list[str], list[str]]:
    by_name: dict[str, list[str]] = {}
    for key, primitive in index.items():
        by_name.setdefault(str(primitive.get("name")), []).append(key)
    found: list[str] = []
    unresolved: list[str] = []
    for name in names:
        if name in index:
            found.append(name)
            continue
        matches = by_name.get(name, [])
        if len(matches) == 1:
            found.append(matches[0])
        elif not matches:
            unresolved.append(f"{name} (the server does not offer it)")
        else:
            unresolved.append(f"{name} (more than one thing has this name: {', '.join(matches)})")
    return found, unresolved


def _candidate(
    key: str,
    primitive: Mapping[str, object],
    policy: Mapping[str, object],
    *,
    requested: bool,
    selected: bool,
    changed: bool,
) -> Candidate:
    name = str(primitive.get("name"))
    classification = str(policy.get("classification"))
    is_tool = primitive.get("type") == "tool"
    hinted = _annotations(primitive).get("readOnlyHint") is True
    description = primitive.get("description")
    return Candidate(
        key=key,
        name=name,
        kind=str(primitive.get("type")),
        classification=classification,
        evidence=_evidence(primitive, classification),
        description=description[:2000] if isinstance(description, str) else "",
        public_operation_id=str(policy.get("publicOperationId")),
        requested=requested,
        selected=selected,
        suggested=(
            is_tool
            and classification == "write"
            and not hinted
            and not selected
            and name_suggests_read(name)
        ),
        reviewable=is_tool and classification == "write" and not hinted,
        changed=changed,
    )


def _evidence(primitive: Mapping[str, object], classification: str) -> str:
    if primitive.get("type") != "tool":
        return "It can only be read."
    annotations = _annotations(primitive)
    if annotations.get("readOnlyHint") is True:
        return "The server marks it read-only."
    if annotations.get("destructiveHint") is True:
        return "The server marks it destructive."
    if classification == "sensitive_write":
        return "Its name suggests it removes or overwrites something."
    if classification == "read":
        return "The owner checked that it only reads."
    return "The server gives no hint, so it is treated as a change."


def _annotations(primitive: Mapping[str, object]) -> Mapping[str, object]:
    value = primitive.get("annotations")
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}


def _order(primitive: Mapping[str, object]) -> tuple[int, str]:
    return _KIND_ORDER.get(str(primitive.get("type")), 9), str(primitive.get("name"))


def _str(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise InvalidStateError(f"Stored proposal field {key} is invalid")
    return item


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidStateError("Stored proposal is invalid")
    return cast(Mapping[str, object], value)


def _sequence(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise InvalidStateError("Stored proposal is invalid")
    return cast(Sequence[object], value)
