"""The owner's gate: which writes wait for the owner, and the exact check before one runs.

The decision is deterministic and made from the published policy and what this connection has
already done. No model is involved, and nothing the agent says can change it.

A tool's policy may carry ``ownerControls``:

``approval``
    ``always`` holds every call, ``risk`` holds a call only when a signal fires, ``never`` lets
    it run. Reads are never held. Destructive tools (sensitive writes) are always held.
``recipients``
    Argument paths that say where something is sent (``to``, ``attendees``). A value the owner
    has not accepted before is a signal.
``knownRecipients``
    Patterns (``*@household.test``) that count as already accepted.
``untrusted``
    On a read: what it returns was written by someone else. A write soon after such a read is a
    signal, because that is how a hidden instruction reaches an action.
``arguments``
    Hard limits: for an argument path, patterns the value must match. A value outside them is
    refused outright, not held.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import cast

from ..catalog.canonical import thaw_json
from ..domain.errors import ArgumentNotAllowedError
from ..domain.models import PublishedPrimitive
from ..persistence.owner_store import APPROVED, DENIED, PENDING, USED, ApprovalRequest, OwnerStore
from ..persistence.sqlite_repository import SqliteControlPlaneRepository

APPROVAL_MODES = ("always", "risk", "never")
DEFAULT_UNTRUSTED_WINDOW_SECONDS = 1800
DEFAULT_REQUEST_TTL_SECONDS = 900
MAX_PENDING_PER_CONNECTION = 20


@dataclass(frozen=True, slots=True)
class OwnerControls:
    approval: str = "always"
    recipients: tuple[str, ...] = ()
    known_recipients: tuple[str, ...] = ()
    untrusted: bool = False
    untrusted_window_seconds: int = DEFAULT_UNTRUSTED_WINDOW_SECONDS
    arguments: Mapping[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])


def controls_of(primitive: PublishedPrimitive) -> OwnerControls:
    """The owner controls in a published policy, with defaults for anything left out."""
    policy = cast(Mapping[str, object], thaw_json(primitive.policy))
    raw = policy.get("ownerControls")
    if not isinstance(raw, Mapping):
        return OwnerControls()
    data = cast(Mapping[str, object], raw)
    arguments = data.get("arguments")
    return OwnerControls(
        approval=str(data.get("approval", "always")),
        recipients=_strings(data.get("recipients")),
        known_recipients=_strings(data.get("knownRecipients")),
        untrusted=data.get("untrusted") is True,
        untrusted_window_seconds=_window(data.get("untrustedWindowSeconds")),
        arguments={
            str(path): _strings(patterns)
            for path, patterns in cast(Mapping[str, object], arguments).items()
        }
        if isinstance(arguments, Mapping)
        else {},
    )


def check_arguments(primitive: PublishedPrimitive, arguments: Mapping[str, object]) -> None:
    """Refuse a call whose arguments are outside the owner's hard limits."""
    for path, patterns in controls_of(primitive).arguments.items():
        for value in values_at(arguments, path):
            if not any(re.fullmatch(pattern, value, re.IGNORECASE) for pattern in patterns):
                raise ArgumentNotAllowedError(
                    f"The owner has not allowed {_shown(value)} for '{path}' on "
                    f"{primitive.public_operation_id}. Do not repeat the call: tell the user."
                )


def values_at(arguments: Mapping[str, object], path: str) -> list[str]:
    """The text values at a dotted path, flattening lists. Anything that is not text is skipped."""
    nodes: list[object] = [arguments]
    for part in path.split("."):
        following: list[object] = []
        for node in nodes:
            if isinstance(node, Mapping) and part in node:
                following.append(cast(Mapping[str, object], node)[part])
            elif isinstance(node, list):
                following.extend(
                    cast(Mapping[str, object], item)[part]
                    for item in cast(list[object], node)
                    if isinstance(item, Mapping) and part in item
                )
        nodes = following
    found: list[str] = []
    for node in nodes:
        if isinstance(node, str):
            found.append(node)
        elif isinstance(node, list):
            found.extend(item for item in cast(list[object], node) if isinstance(item, str))
    return found


@dataclass(frozen=True, slots=True)
class Decision:
    """What the gate decided about one write."""

    outcome: str  # "run", "wait" or "denied"
    approval_id: str | None = None
    reasons: tuple[str, ...] = ()
    request: ApprovalRequest | None = None
    token: str | None = None  # the link token for the chat (new or reissued)
    owner_token: str | None = None  # the owner's own link token, only when just created


class OwnerGate:
    def __init__(
        self,
        repository: SqliteControlPlaneRepository,
        store: OwnerStore,
        *,
        request_ttl_seconds: int = DEFAULT_REQUEST_TTL_SECONDS,
    ) -> None:
        self._repository = repository
        self._store = store
        self._ttl = request_ttl_seconds

    @property
    def store(self) -> OwnerStore:
        return self._store

    def decide(
        self,
        *,
        primitive: PublishedPrimitive,
        server_id: str,
        connection_id: str,
        arguments: Mapping[str, object],
        content_digest: str,
        operation_id: str,
        now: str,
    ) -> Decision:
        controls = controls_of(primitive)
        signals = self._signals(primitive, controls, server_id, connection_id, arguments, now)
        needs_owner = (
            primitive.classification == "sensitive_write"
            or controls.approval == "always"
            or (controls.approval == "risk" and bool(signals))
        )
        if not needs_owner:
            return Decision("run", approval_id=f"auto:{operation_id}")

        reasons = tuple(signals) or ("The owner asked to approve every use of this tool",)
        earlier = self._store.latest_for(connection_id, content_digest)
        if earlier is not None and not earlier.expired(now):
            if earlier.status == APPROVED:
                self._store.use(earlier.request_id, operation_id, now)
                return Decision("run", approval_id=f"owner:{earlier.request_id}")
            if earlier.status == DENIED:
                return Decision("denied", reasons=earlier.reasons, request=earlier)
            if earlier.status == PENDING:
                token = self._store.reissue_token(earlier.request_id)
                return Decision("wait", reasons=earlier.reasons, request=earlier, token=token)
            # A used approval does not carry over: this is a second, separate action.
            assert earlier.status == USED
        if self._store.count_pending(connection_id, now) >= MAX_PENDING_PER_CONNECTION:
            raise ArgumentNotAllowedError(
                "Too many actions are already waiting for the owner. Do not send more: "
                "tell the user."
            )
        recipients = tuple(
            value for path in controls.recipients for value in values_at(arguments, path)
        )
        created, token, owner_token = self._store.create_request(
            connection_id=connection_id,
            server_id=server_id,
            public_operation_id=primitive.public_operation_id,
            classification=primitive.classification,
            content_digest=content_digest,
            arguments=arguments,
            reasons=reasons,
            recipients=recipients,
            now=now,
            ttl_seconds=self._ttl,
        )
        return Decision(
            "wait", reasons=reasons, request=created, token=token, owner_token=owner_token
        )

    # --- risk signals ------------------------------------------------------------------

    def _signals(
        self,
        primitive: PublishedPrimitive,
        controls: OwnerControls,
        server_id: str,
        connection_id: str,
        arguments: Mapping[str, object],
        now: str,
    ) -> list[str]:
        found: list[str] = []
        if primitive.classification == "sensitive_write":
            found.append("This cannot be undone")

        known = self._store.known_recipients(connection_id)
        strangers = [
            value
            for path in controls.recipients
            for value in values_at(arguments, path)
            if value.lower() not in known and not _matches_any(value, controls.known_recipients)
        ]
        if strangers:
            found.append(
                "It goes to someone you have not approved before: "
                + ", ".join(sorted(set(strangers)))
            )

        untrusted = self._untrusted_reads(server_id)
        if untrusted:
            window = max(seconds for _, seconds in untrusted)
            since = (_time(now) - timedelta(seconds=window)).isoformat()
            recent = self._store.recently_completed(connection_id, [n for n, _ in untrusted], since)
            if recent:
                found.append(
                    "It follows reading content written by someone else (" + ", ".join(recent) + ")"
                )
        return found

    def _untrusted_reads(self, server_id: str) -> list[tuple[str, int]]:
        catalog = self._repository.get_active_catalog(server_id)
        return [
            (item.public_operation_id, controls_of(item).untrusted_window_seconds)
            for item in catalog.primitives
            if controls_of(item).untrusted
        ]


def _matches_any(value: str, patterns: Sequence[str]) -> bool:
    lowered = value.lower()
    return any(fnmatch.fnmatchcase(lowered, pattern.lower()) for pattern in patterns)


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(item for item in cast(list[object], value) if isinstance(item, str))
    return ()


def _window(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_UNTRUSTED_WINDOW_SECONDS


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _shown(value: str) -> str:
    return repr(value if len(value) <= 80 else value[:77] + "...")
