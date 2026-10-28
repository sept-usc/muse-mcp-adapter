"""What of the owner's data may enter the agent's context, and where a sealed value may go.

A tool's policy may carry a ``release`` block:

``fields``
    For each place in a tool's result (a path such as ``$.contacts[*].email``), a label and a
    rung of the disclosure ladder: ``full`` (the agent sees it), ``handle`` (the agent sees an
    opaque token, ``[contact.email:k3f9a2bq]``, and the adapter keeps the value), ``withhold``
    (the agent never sees it), or ``redact`` (patterns such as card and national-ID numbers are
    blanked out of free text).
``slots``
    For each argument of a tool, the labels whose handles may be filled in there. A handle in any
    other place is refused: a sealed email address can be the ``to`` of a message and nothing else.
``patterns``
    The patterns used by ``redact``, and applied to every text block of the result.
``text``
    For servers that answer in prose rather than fields: find email addresses (``email``) or phone
    numbers (``phone``), or whatever the owner's own ``regex`` (a ``pattern`` of up to 200
    characters, such as a ``Notes:`` line) matches anywhere in the result's text, and give each a
    label and a rung of ``handle`` or ``withhold``. ``keep`` lists values that stay readable, such
    as the owner's own address. The rule covers every string in the result, structured or not.

``reveal``
    Labels the owner may choose to show the agent, one value at a time. The value stays sealed
    until the agent asks to see a particular handle and the owner approves that one value.

The adapter has no model and does not decide what is sensitive; the owner's policy does, field by
field. What it adds is that the rule is enforced on the way out and on the way back in, where the
agent cannot argue with it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any, Protocol, cast

from ..catalog.canonical import freeze_json, thaw_json
from ..domain.errors import HandleNotAllowedError, HandleUnknownError
from ..domain.models import DataPlaneRequest, DataPlaneResponse, PublishedPrimitive

RUNGS = ("full", "handle", "withhold", "redact")
PATTERN_NAMES = ("card", "ssn", "iban")
MIN_SUBSTITUTION_LENGTH = 4
WILDCARD = "*"

HANDLE = re.compile(r"\[([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+):([a-z2-7]{8})\]")
_PATH = re.compile(r"^\$(?:\.[A-Za-z_][\w-]*|\[\*\])+$")
_STEP = re.compile(r"\.([A-Za-z_][\w-]*)|\[\*\]")
_CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
_SSN = re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_PHONE = re.compile(
    r"(?<![\w+.])\+\d[\d .()-]{6,16}\d(?!\w)"
    r"|(?<![\w.-])\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\w-])"
)
TEXT_FINDERS = {"email": _EMAIL, "phone": _PHONE}
TEXT_RUNGS = ("handle", "withhold")
_REMOVE = object()


@dataclass(frozen=True, slots=True)
class FieldRule:
    path: tuple[str, ...]
    label: str
    rung: str


@dataclass(frozen=True, slots=True)
class TextRule:
    find: str
    label: str
    rung: str
    keep: frozenset[str] = frozenset()
    pattern: str = ""


@dataclass(frozen=True, slots=True)
class ReleasePolicy:
    fields: tuple[FieldRule, ...] = ()
    slots: Mapping[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    patterns: tuple[str, ...] = ()
    text: tuple[TextRule, ...] = ()
    reveal: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return bool(self.fields or self.slots or self.patterns or self.text or self.reveal)


class Vault(Protocol):
    def handle_for(self, connection_id: str, label: str, value: str, now: str) -> str: ...

    def lookup(self, connection_id: str, handle_id: str, now: str) -> tuple[str, str] | None: ...


class Ledger(Protocol):
    def record(
        self, kind: str, *, connection_id: str, operation: str, label: str, detail: str, now: str
    ) -> None: ...


def release_of(primitive: PublishedPrimitive) -> ReleasePolicy:
    policy = cast(Mapping[str, object], thaw_json(primitive.policy))
    raw = policy.get("release")
    if not isinstance(raw, Mapping):
        return ReleasePolicy()
    data = cast(Mapping[str, object], raw)
    fields: list[FieldRule] = []
    for item in cast(list[object], data.get("fields") or []):
        if isinstance(item, Mapping):
            rule = cast(Mapping[str, object], item)
            fields.append(
                FieldRule(parse_path(str(rule["path"])), str(rule["label"]), str(rule["rung"]))
            )
    slots_raw = data.get("slots")
    slots = (
        {
            str(name): tuple(str(label) for label in cast(list[object], labels))
            for name, labels in cast(Mapping[str, object], slots_raw).items()
        }
        if isinstance(slots_raw, Mapping)
        else {}
    )
    patterns = data.get("patterns")
    text: list[TextRule] = []
    for item in cast(list[object], data.get("text") or []):
        if isinstance(item, Mapping):
            rule = cast(Mapping[str, object], item)
            keep = rule.get("keep")
            text.append(
                TextRule(
                    str(rule["find"]),
                    str(rule["label"]),
                    str(rule["rung"]),
                    frozenset(str(v).lower() for v in cast(list[object], keep))
                    if isinstance(keep, list)
                    else frozenset(),
                    str(rule.get("pattern") or ""),
                )
            )
    reveal = data.get("reveal")
    return ReleasePolicy(
        fields=tuple(fields),
        slots=slots,
        patterns=tuple(str(p) for p in cast(list[object], patterns))
        if isinstance(patterns, list)
        else (),
        text=tuple(text),
        reveal=tuple(str(label) for label in cast(list[object], reveal))
        if isinstance(reveal, list)
        else (),
    )


def parse_path(path: str) -> tuple[str, ...]:
    if not _PATH.fullmatch(path):
        raise ValueError(f"Not a supported path: {path!r}")
    return tuple(m.group(1) or WILDCARD for m in _STEP.finditer(path))


# --- closing the door on the way out ---------------------------------------------------------


@dataclass(slots=True)
class Sealed:
    structured: dict[str, Any] | None
    texts: list[str]
    sealed: int = 0
    withheld: int = 0
    redacted: int = 0


def seal_result(
    policy: ReleasePolicy,
    structured: Mapping[str, object] | None,
    texts: Sequence[str],
    *,
    vault: Vault,
    ledger: Ledger | None,
    connection_id: str,
    operation: str,
    now: str,
    echoes: Mapping[str, str] | None = None,
) -> Sealed:
    """Apply the policy to one tool result: its structured content and its text blocks.

    ``echoes`` are values the adapter filled in from handles on the way in; any that the tool
    repeats in its result go back to being handles.
    """
    counts = {"sealed": 0, "withheld": 0, "redacted": 0}
    replacements: dict[str, str] = {}
    original = thaw_json(structured) if structured is not None else None
    rewritten: Any = thaw_json(structured) if structured is not None else None

    def note(kind: str, label: str, detail: str) -> None:
        counts[kind] += 1
        if ledger is not None:
            ledger.record(
                kind, connection_id=connection_id, operation=operation, label=label,
                detail=detail, now=now,
            )  # fmt: skip

    def apply(value: Any, rule: FieldRule) -> Any:
        if rule.rung == "full" or value is None:
            return value
        if rule.rung == "withhold":
            if isinstance(value, str) and value:
                replacements[value] = ""
            note("withheld", rule.label, "field removed")
            return _REMOVE
        if not isinstance(value, str) or not value:
            return value
        if rule.rung == "handle":
            token = vault.handle_for(connection_id, rule.label, value, now)
            replacements[value] = token
            note("sealed", rule.label, token)
            return token
        redacted, hits = redact(value, policy.patterns or PATTERN_NAMES)
        for name in hits:
            note("redacted", f"pattern.{name}", name)
        if redacted != value:
            replacements[value] = redacted
        return redacted

    def scrub(text: str, record: Any = note) -> str:
        for rule in policy.text:
            text = _apply_text_rule(rule, text, vault, connection_id, now, record)
        return text

    def quiet(kind: str, label: str, detail: str) -> None:
        """The structured copy of a result is sealed too, but its text copy is the one logged."""

    if rewritten is not None:
        for rule in policy.fields:
            matched = [0]

            def counted(value: Any, rule: FieldRule = rule, matched: list[int] = matched) -> Any:
                matched[0] += 1
                return apply(value, rule)

            rewritten = _walk(rewritten, rule.path, counted)
            if matched[0] == 0 and ledger is not None:
                # A rule that touched nothing is usually a path that does not fit the result, and
                # then the field it was meant to protect goes through unprotected.
                ledger.record(
                    "unmatched", connection_id=connection_id, operation=operation,
                    label=rule.label, detail="no field matched " + _path_text(rule.path), now=now,
                )  # fmt: skip
        if echoes:
            rewritten = _reseal(rewritten, echoes, note)
        if policy.text:
            record = quiet if texts else note
            rewritten = _map_strings(
                rewritten, lambda value: _clean(scrub(value, record), policy, record)
            )

    new_texts: list[str] = []
    for text in texts:
        new_texts.append(
            _rewrite_text(
                text, original, rewritten, replacements, policy, note, echoes or {}, scrub
            )
        )
    return Sealed(rewritten, new_texts, counts["sealed"], counts["withheld"], counts["redacted"])


def _reseal(node: Any, echoes: Mapping[str, str], note: Any) -> Any:
    """Put handles back wherever a filled-in value appears again in a result."""
    if isinstance(node, str):
        for value in sorted(echoes, key=len, reverse=True):
            if len(value) >= MIN_SUBSTITUTION_LENGTH and value in node:
                node = node.replace(value, echoes[value])
                note("sealed", HANDLE.sub(r"\1", echoes[value]), "echo sealed again")
        return node
    if isinstance(node, list):
        return [_reseal(item, echoes, note) for item in cast(list[Any], node)]
    if isinstance(node, dict):
        return {k: _reseal(v, echoes, note) for k, v in cast(dict[str, Any], node).items()}
    return node


def _apply_text_rule(
    rule: TextRule,
    text: str,
    vault: Vault,
    connection_id: str,
    now: str,
    note: Any,
) -> str:
    finder = TEXT_FINDERS[rule.find] if rule.find != "regex" else _compiled(rule.pattern)

    def swap(match: re.Match[str]) -> str:
        value = match.group(0)
        if value.lower() in rule.keep:
            return value
        if rule.rung == "withhold":
            note("withheld", rule.label, "text removed")
            return "[withheld]"
        token = vault.handle_for(connection_id, rule.label, value, now)
        note("sealed", rule.label, token)
        return token

    return finder.sub(swap, text)


@lru_cache(maxsize=64)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _clean(text: str, policy: ReleasePolicy, note: Any) -> str:
    cleaned, hits = redact(text, policy.patterns or PATTERN_NAMES)
    for name in hits:
        note("redacted", f"pattern.{name}", name)
    return cleaned


def _map_strings(node: Any, change: Callable[[str], str]) -> Any:
    if isinstance(node, str):
        return change(node)
    if isinstance(node, list):
        return [_map_strings(item, change) for item in cast(list[Any], node)]
    if isinstance(node, dict):
        return {k: _map_strings(v, change) for k, v in cast(dict[str, Any], node).items()}
    return node


def _path_text(path: tuple[str, ...]) -> str:
    return "$" + "".join("[*]" if part == WILDCARD else f".{part}" for part in path)


def _walk(node: Any, path: tuple[str, ...], leaf: Callable[[Any], Any]) -> Any:
    """Return ``node`` with ``leaf`` applied at ``path``. ``_REMOVE`` from ``leaf`` deletes."""
    if not path:
        return leaf(node)
    head, rest = path[0], path[1:]
    if head == WILDCARD:
        if not isinstance(node, list):
            return node
        out = [_walk(item, rest, leaf) for item in cast(list[Any], node)]
        return [item for item in out if item is not _REMOVE]
    if isinstance(node, dict) and head in node:
        data = cast(dict[str, Any], node)
        changed = _walk(data[head], rest, leaf)
        if changed is _REMOVE:
            return {k: v for k, v in data.items() if k != head}
        updated: dict[str, Any] = dict(data)
        updated[head] = changed
        return updated
    return cast(Any, node)


def _rewrite_text(
    text: str,
    original: Any,
    rewritten: Any,
    replacements: Mapping[str, str],
    policy: ReleasePolicy,
    note: Any,
    echoes: Mapping[str, str],
    scrub: Callable[[str], str] = lambda text: text,
) -> str:
    if original is not None:
        try:
            if json.loads(text) == original:
                return json.dumps(rewritten, indent=2)
        except ValueError:
            pass
    for value in sorted(replacements, key=len, reverse=True):
        if len(value) >= MIN_SUBSTITUTION_LENGTH:
            text = text.replace(value, replacements[value])
    for value in sorted(echoes, key=len, reverse=True):
        if len(value) >= MIN_SUBSTITUTION_LENGTH:
            text = text.replace(value, echoes[value])
    return _clean(scrub(text), policy, note)


def redact(text: str, patterns: Sequence[str]) -> tuple[str, list[str]]:
    """Blank out card, national-ID and bank-account numbers. Returns the text and what was hit."""
    hits: list[str] = []

    def card(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            hits.append("card")
            return "[redacted:card]"
        return match.group(0)

    def blank(name: str) -> Any:
        def sub(_: re.Match[str]) -> str:
            hits.append(name)
            return f"[redacted:{name}]"

        return sub

    if "card" in patterns:
        text = _CARD.sub(card, text)
    if "ssn" in patterns:
        text = _SSN.sub(blank("ssn"), text)
    if "iban" in patterns:
        text = _IBAN.sub(blank("iban"), text)
    return text, hits


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        number = int(char)
        if index % 2 == 1:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10 == 0


# --- opening the door on the way in ----------------------------------------------------------


def open_arguments(
    policy: ReleasePolicy,
    arguments: Mapping[str, object],
    *,
    vault: Vault,
    ledger: Ledger | None,
    connection_id: str,
    operation: str,
    now: str,
    filled: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Fill sealed values back in where the policy allows, and refuse them anywhere else.

    ``filled`` collects what was filled in (value -> handle) so a result that echoes it back can be
    sealed again.
    """

    def fill(value: Any, slot: str) -> Any:
        if isinstance(value, str):
            return HANDLE.sub(lambda m: one(m, slot), value)
        if isinstance(value, list):
            return [fill(item, slot) for item in cast(list[Any], value)]
        if isinstance(value, dict):
            return {
                key: fill(item, f"{slot}.{key}" if slot else str(key))
                for key, item in cast(dict[str, Any], value).items()
            }
        return value

    def one(match: re.Match[str], slot: str) -> str:
        label, handle_id = match.group(1), match.group(2)
        found = vault.lookup(connection_id, handle_id, now)
        if found is None or found[0] != label:
            _record("refused", label, f"unknown or expired handle in {slot}")
            raise HandleUnknownError(
                f"{match.group(0)} is not a value this connection was given, or it has expired. "
                "Do not make up or edit sealed values: read them again from the source."
            )
        allowed = policy.slots.get(slot, ())
        if label not in allowed:
            where = ", ".join(sorted(s for s, labels in policy.slots.items() if label in labels))
            _record("refused", label, f"not allowed in {slot}")
            raise HandleNotAllowedError(
                f"{match.group(0)} is a sealed value and cannot be used in '{slot}'. "
                + (
                    f"It can only be used in: {where}. "
                    if where
                    else "It cannot be used as an argument of this operation. "
                )
                + "Remove it and use only what the owner asked you to share."
            )
        _record("rehydrated", label, f"{handle_id} into {slot}")
        if filled is not None:
            filled[found[1]] = match.group(0)
        return found[1]

    def _record(kind: str, label: str, detail: str) -> None:
        if ledger is not None:
            ledger.record(
                kind, connection_id=connection_id, operation=operation, label=label,
                detail=detail, now=now,
            )  # fmt: skip

    return {key: fill(value, str(key)) for key, value in arguments.items()}


def contains_handle(value: object) -> bool:
    if isinstance(value, str):
        return HANDLE.search(value) is not None
    if isinstance(value, list):
        return any(contains_handle(item) for item in cast(list[object], value))
    if isinstance(value, Mapping):
        return any(contains_handle(item) for item in cast(Mapping[str, object], value).values())
    return False


class ReleaseEngine:
    """Applies each tool's release policy to its requests and its results."""

    def __init__(self, vault: Vault, ledger: Ledger | None, clock: Callable[[], str]) -> None:
        self._vault = vault
        self._ledger = ledger
        self._clock = clock

    def reveal_labels(self, primitives: Iterable[PublishedPrimitive]) -> frozenset[str]:
        """The labels any tool in the catalog lets the owner choose to reveal."""
        return frozenset(label for item in primitives for label in release_of(item).reveal)

    def find(self, connection_id: str, handle: str) -> tuple[str, str, str] | None:
        """(label, value, handle id) for a handle this connection holds, else None."""
        match = HANDLE.fullmatch(handle.strip())
        if match is None:
            return None
        found = self._vault.lookup(connection_id, match.group(2), self._clock())
        if found is None or found[0] != match.group(1):
            return None
        return found[0], found[1], match.group(2)

    def note(
        self, kind: str, *, connection_id: str, operation: str, label: str, detail: str
    ) -> None:
        if self._ledger is not None:
            self._ledger.record(
                kind, connection_id=connection_id, operation=operation, label=label,
                detail=detail, now=self._clock(),
            )  # fmt: skip

    def open_request(
        self, request: DataPlaneRequest, primitive: PublishedPrimitive
    ) -> DataPlaneRequest:
        """The request with sealed values filled in where allowed. It fails if one is misused."""
        policy = release_of(primitive)
        if not contains_handle(request.arguments):
            return request
        filled: dict[str, str] = {}
        opened = open_arguments(
            policy,
            cast(Mapping[str, object], thaw_json(request.arguments)),
            vault=self._vault,
            ledger=self._ledger,
            connection_id=request.connection_id,
            operation=primitive.public_operation_id,
            now=self._clock(),
            filled=filled,
        )
        return replace(
            request,
            arguments=cast(Mapping[str, object], freeze_json(opened)),
            filled=filled,
        )

    def seal_response(
        self,
        request: DataPlaneRequest,
        primitive: PublishedPrimitive,
        response: DataPlaneResponse,
    ) -> DataPlaneResponse:
        policy = release_of(primitive)
        if not policy.active and not request.filled:
            return response
        texts = [item.text for item in response.contents if item.kind == "text"]
        sealed = seal_result(
            policy,
            response.structured_content,
            texts,
            vault=self._vault,
            ledger=self._ledger,
            connection_id=request.connection_id,
            operation=primitive.public_operation_id,
            now=self._clock(),
            echoes=request.filled,
        )
        replacement = iter(sealed.texts)
        contents = tuple(
            replace(item, text=next(replacement)) if item.kind == "text" else item
            for item in response.contents
        )
        structured = (
            cast(Mapping[str, object], freeze_json(sealed.structured))
            if sealed.structured is not None
            else None
        )
        return replace(response, structured_content=structured, contents=contents)
