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
from ..policy import expiry
from ..policy.argument_limits import MAX_LIMITS_PER_TOOL, describe_limit, no_looser
from .models import ArgumentLimit, SetupRequest

VERSION = 1
MAX_OFFERED = 2000  # a page of more than this is not a review anyone can do
CLASS_RANK: Mapping[str, int] = {"read": 0, "write": 1, "sensitive_write": 2}
_KIND_ORDER = {"tool": 0, "resource": 1, "resource_template": 2, "prompt": 3}
_IGNORED_WHEN_COMPARING = frozenset(
    {"classification", "timeoutSeconds", "maxResultBytes", "argumentPatterns", "expiresAt"}
)
_LIMITS = ("timeoutSeconds", "maxResultBytes")
_MAX_UNLIMITED_NOTES = 10


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
    limits: tuple[tuple[str, str], ...] = ()  # (argument, in words) the owner limits it to

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
            "limits": [list(pair) for pair in self.limits],
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
            limits=tuple(
                (str(pair[0]), str(pair[1]))
                for pair in (_sequence(item) for item in _sequence(value.get("limits") or []))
                if len(pair) == 2
            ),
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
    expires_in_hours: int | None = None  # access ends this long after approval
    inherited_expiry: str | None = None  # the end a connection already has, when none is asked
    inherited_ended: bool = False  # that end has already passed (set when the owner is shown it)

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
            "expiresInHours": self.expires_in_hours,
            "inheritedExpiry": self.inherited_expiry,
            "inheritedEnded": self.inherited_ended,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> Computed:
        candidates = tuple(
            Candidate.from_json(_mapping(item)) for item in _sequence(value.get("candidates"))
        )
        raw_policies = _mapping(value.get("policies"))
        base = value.get("baseVersion")
        hours, inherited = value.get("expiresInHours"), value.get("inheritedExpiry")
        return cls(
            candidates=candidates,
            policies={key: _mapping(entry) for key, entry in raw_policies.items()},
            unresolved=tuple(str(item) for item in _sequence(value.get("unresolved"))),
            gone=tuple(str(item) for item in _sequence(value.get("gone"))),
            unenforced=tuple(str(item) for item in _sequence(value.get("unenforced"))),
            base_version=base if isinstance(base, str) else None,
            digest=_str(value, "digest"),
            expires_in_hours=hours
            if isinstance(hours, int) and not isinstance(hours, bool)
            else None,
            inherited_expiry=inherited if isinstance(inherited, str) else None,
            inherited_ended=value.get("inheritedEnded") is True,
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
    policies: dict[str, dict[str, object]] = {}
    dropped_notes: list[str] = []
    for key, draft in drafts.items():
        if key in live and key not in changed_keys:
            entry = dict(live[key])
        else:
            entry = dict(draft)
            if key in live:  # the server changed it: draft afresh, keep the name Muse knows
                entry["publicOperationId"] = live[key].get("publicOperationId")
                kept_limits, dropped = _surviving_limits(live[key], index[key])
                if kept_limits:
                    entry["argumentPatterns"] = kept_limits
                dropped_notes.extend(dropped)
        entry["allowed"] = key in chosen
        if key in chosen and index[key].get("type") == "tool":
            if request.timeout_seconds is not None:
                entry["timeoutSeconds"] = request.timeout_seconds
            if request.max_result_bytes is not None:
                entry["maxResultBytes"] = request.max_result_bytes
        policies[key] = entry
    limit_notes = apply_limits(policies, index, request.limits)

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
    unenforced = (
        *((f"Not enforced: {request.notes}",) if request.notes else ()),
        *dropped_notes,
        *unlimited_notes(policies, index, chosen),
    )
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
        unresolved=tuple(missing_allow + missing_exclude + limit_notes),
        gone=tuple(sorted(key for key in live if key not in index)),
        unenforced=tuple(unenforced),
        base_version=base_version,
        digest=digest(payload),
        expires_in_hours=request.expires_in_hours,
        inherited_expiry=None if request.expires_in_hours is not None else _earliest_end(live),
    )


def _earliest_end(live: Mapping[str, Mapping[str, object]]) -> str | None:
    """The soonest end among what is live now, so that adding to a connection does not extend it."""
    ends = [
        (instant, text)
        for entry in live.values()
        if isinstance(text := entry.get("expiresAt"), str)
        and (instant := expiry.parse(text)) is not None
    ]
    return min(ends)[1] if ends else None


def finalize(
    computed: Computed,
    chosen: Collection[str],
    reviewed_read: Collection[str],
    expires_at: str | None = None,
) -> Final:
    """Apply the owner's choices to a compiled proposal.

    The owner may remove anything, add anything the server offers, and mark a change-by-default
    tool read-only after checking it. Nothing outside what the server offers can be chosen.
    ``expires_at`` is when access ends if the request asked for a time limit; without it, an
    operation with no end keeps the end that the connection already has.
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
        if key in chosen_set:
            if expires_at is not None:
                entry["expiresAt"] = expires_at
            elif computed.inherited_expiry and "expiresAt" not in entry:
                entry["expiresAt"] = computed.inherited_expiry
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
        digest=digest(
            {
                "proposal": computed.digest,
                "chosen": sorted(chosen_set),
                "read": marked,
                "expiresAt": expires_at or computed.inherited_expiry,
            }
        ),
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


def apply_limits(
    entries: dict[str, dict[str, object]],
    index: Mapping[str, Mapping[str, object]],
    limits: Sequence[ArgumentLimit],
) -> list[str]:
    """Put the limits on the tools they name. Says what could not be applied, and why."""
    notes: list[str] = []
    for limit in limits:
        pattern = limit.regex()
        applied = False
        named = False
        for key, primitive in index.items():
            if primitive.get("type") != "tool" or key not in entries:
                continue
            name = str(primitive.get("name"))
            known_as = (name, key, str(entries[key].get("publicOperationId")))
            if limit.operation != "*" and limit.operation not in known_as:
                continue
            named = True
            if limit.argument not in _declared_arguments(primitive):
                if limit.operation != "*":
                    notes.append(f"{limit.describe()} (the operation has no such argument)")
                continue
            current = entries[key].get("argumentPatterns")
            patterns = (
                dict(cast(Mapping[str, str], current)) if isinstance(current, Mapping) else {}
            )
            patterns[limit.argument] = pattern
            if len(patterns) > MAX_LIMITS_PER_TOOL:
                notes.append(f"{limit.describe()} (too many limits on one operation)")
                continue
            entries[key]["argumentPatterns"] = patterns
            applied = True
        if not named:
            notes.append(f"{limit.describe()} (the server does not offer that operation)")
        elif not applied and limit.operation == "*":
            notes.append(f"{limit.describe()} (no operation has that argument)")
    return notes


def _surviving_limits(
    live: Mapping[str, object], primitive: Mapping[str, object]
) -> tuple[dict[str, str], list[str]]:
    """The limits of an operation the server has since redefined: kept while its argument exists."""
    raw = live.get("argumentPatterns")
    if not isinstance(raw, Mapping):
        return {}, []
    declared = _declared_arguments(primitive)
    kept: dict[str, str] = {}
    dropped: list[str] = []
    name = str(primitive.get("name"))
    for argument, pattern in sorted(
        cast(Mapping[object, object], raw).items(), key=lambda item: str(item[0])
    ):
        if str(argument) in declared:
            kept[str(argument)] = str(pattern)
        else:
            dropped.append(
                f"{name}: the limit on {argument} ({describe_limit(str(pattern))}) is gone "
                "because the server's new definition has no such argument."
            )
    return kept, dropped


def unlimited_notes(
    policies: Mapping[str, Mapping[str, object]],
    index: Mapping[str, Mapping[str, object]],
    chosen: Collection[str],
) -> list[str]:
    """Where an argument is limited on some chosen operations but not on another that takes it."""
    limited: dict[str, set[str]] = {}
    tools = sorted(
        (key for key in chosen if key in index and index[key].get("type") == "tool"),
        key=lambda key: str(index[key].get("name")),
    )
    for key in tools:
        raw = policies[key].get("argumentPatterns")
        for argument in cast(Mapping[str, object], raw) if isinstance(raw, Mapping) else ():
            limited.setdefault(str(argument), set()).add(str(index[key].get("name")))
    notes: list[str] = []
    for key in tools:
        raw = policies[key].get("argumentPatterns")
        own: set[str] = set(cast(Mapping[str, object], raw)) if isinstance(raw, Mapping) else set()
        for argument in sorted((_declared_arguments(index[key]) & limited.keys()) - own):
            where = ", ".join(sorted(limited[argument])[:3])
            notes.append(
                f"{index[key].get('name')} takes {argument}, which is limited on {where} but "
                f"is not limited on {index[key].get('name')}."
            )
    if len(notes) > _MAX_UNLIMITED_NOTES:
        extra = len(notes) - _MAX_UNLIMITED_NOTES
        notes = [*notes[:_MAX_UNLIMITED_NOTES], f"...and {extra} more like these."]
    return notes


def _declared_arguments(primitive: Mapping[str, object]) -> set[str]:
    schema = primitive.get("inputSchema")
    properties = (
        cast(Mapping[str, object], schema).get("properties")
        if isinstance(schema, Mapping)
        else None
    )
    return (
        {str(name) for name in cast(Mapping[object, object], properties)}
        if isinstance(properties, Mapping)
        else set()
    )


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
        limits=_limits_of(policy),
    )


def _limits_of(policy: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    raw = policy.get("argumentPatterns")
    if not isinstance(raw, Mapping):
        return ()
    return tuple(
        (str(name), describe_limit(str(pattern)))
        for name, pattern in sorted(
            cast(Mapping[object, object], raw).items(), key=lambda x: str(x[0])
        )
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
