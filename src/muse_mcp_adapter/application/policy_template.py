"""Draft an exposure policy from a discovery snapshot for the owner to review.

The draft is deliberately conservative: nothing is exposed unless asked for, tools the server
did not mark read-only are drafted as writes (destructive ones as sensitive writes), and the
owner edits and publishes the result.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import cast
from urllib.parse import urlsplit

from ..domain.constants import primitive_key
from ..domain.models import DiscoverySnapshot
from ..policy.validate_policy import RESERVED_OPERATION_IDS

DEFAULT_RESULT_BYTES = 262_144
# Words in a tool's name that mean it removes, overwrites or hands over something that cannot be
# put back. Such a tool is drafted as a sensitive write (a shorter approval window, and a signal to
# the owner to think twice) even if the server does not say so.
_DESTRUCTIVE_WORDS = frozenset(
    {
        "delete",
        "remove",
        "destroy",
        "drop",
        "purge",
        "truncate",
        "wipe",
        "erase",
        "revoke",
        "terminate",
        "merge",
        "force",
        "overwrite",
        "reset",
        "transfer",
        "deactivate",
    }
)
_NAME_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
_URI_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")


def draft_policies(
    snapshot: DiscoverySnapshot, *, expose_reads: bool = False
) -> list[dict[str, object]]:
    """Return one policy entry per discovered primitive.

    With ``expose_reads`` the primitives that cannot change anything (tools the server marks
    ``readOnlyHint``, resources, templates and prompts) are drafted as allowed. Writes never
    are: the owner must switch them on deliberately.
    """
    used: set[str] = set(RESERVED_OPERATION_IDS)
    policies: list[dict[str, object]] = []
    for primitive in snapshot.primitives:
        kind = str(primitive.get("type"))
        name = str(primitive.get("name"))
        classification = _classification(primitive)
        entry: dict[str, object] = {
            "primitiveKey": primitive_key(primitive),
            "allowed": expose_reads and classification == "read",
            "publicOperationId": _operation_id(name, used),
            "classification": classification,
            "requiredScopes": _scopes(primitive),
        }
        description = primitive.get("description")
        if isinstance(description, str) and description.strip():
            # Pinned: what Muse reads stays as reviewed even if the server later edits its text.
            entry["description"] = description.strip()[:2000]
        if kind == "tool":
            entry["timeoutSeconds"] = 30
            entry["maxResultBytes"] = DEFAULT_RESULT_BYTES
        elif kind in ("resource", "resource_template"):
            entry["resource"] = _resource_policy(primitive)
        elif kind == "prompt":
            entry["prompt"] = {
                "allowedRoles": ["user", "assistant"],
                "allowedEmbeddedMimeTypes": ["text/plain"],
                "maxBytes": DEFAULT_RESULT_BYTES,
            }
        policies.append(entry)
    return policies


def _classification(primitive: Mapping[str, object]) -> str:
    if primitive.get("type") != "tool":
        return "read"
    annotations = primitive.get("annotations")
    hinted = (
        cast(Mapping[str, object], annotations).get("readOnlyHint") is True
        if isinstance(annotations, Mapping)
        else False
    )
    if hinted:
        return "read"
    if _destructive(primitive):
        return "sensitive_write"
    return "write"


def _destructive(primitive: Mapping[str, object]) -> bool:
    """The server says so (destructiveHint), or the name says so (delete_file, mergePullRequest)."""
    annotations = primitive.get("annotations")
    if (
        isinstance(annotations, Mapping)
        and cast(Mapping[str, object], annotations).get("destructiveHint") is True
    ):
        return True
    words = {word.lower() for word in _NAME_WORDS.findall(str(primitive.get("name", "")))}
    return bool(words & _DESTRUCTIVE_WORDS)


def _scopes(primitive: Mapping[str, object]) -> list[str]:
    scopes = primitive.get("requiredScopes")
    if isinstance(scopes, Sequence) and not isinstance(scopes, str):
        return [item for item in cast(Sequence[object], scopes) if isinstance(item, str)]
    return []


def _resource_policy(primitive: Mapping[str, object]) -> dict[str, object]:
    uri = primitive.get("uri") or primitive.get("uriTemplate")
    mime = primitive.get("mimeType")
    policy: dict[str, object] = {
        "allowedMimeTypes": [mime] if isinstance(mime, str) and mime else ["text/plain"],
        "maxBytes": DEFAULT_RESULT_BYTES,
    }
    if isinstance(uri, str):
        match = _URI_SCHEME.match(uri)
        if match:
            scheme = match.group(1).lower()
            policy["allowedUriSchemes"] = [scheme]
            if scheme in ("http", "https"):
                host = urlsplit(uri.replace("{", "x").replace("}", "x")).hostname
                if host:
                    policy["allowedUriHosts"] = [host]
    return policy


def _operation_id(name: str, used: set[str]) -> str:
    candidate = re.sub(r"[^a-z0-9_]+", "_", name.lower().replace("-", "_")).strip("_")
    if not candidate or not candidate[0].isalpha():
        candidate = f"op_{candidate}".strip("_")
    candidate = candidate[:60]
    if len(candidate) < 3:
        candidate = f"{candidate}_op"
    unique = candidate
    counter = 2
    while unique in used:
        suffix = f"_{counter}"
        unique = f"{candidate[: 64 - len(suffix)]}{suffix}"
        counter += 1
    used.add(unique)
    return unique
