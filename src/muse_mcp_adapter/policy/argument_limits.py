"""Limits on the values a tool may be called with, chosen by the owner and enforced on every call.

A limit is a regular expression that the whole value of one argument must match (the same rule the
adapter already applies to resource template arguments). A limit that names allowed values is
stored as the pattern that accepts exactly those values, so there is one mechanism to enforce,
validate and show.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import cast
from urllib.parse import unquote

MAX_LIMITS_PER_TOOL = 10
Scalar = str | int | bool

_ALTERNATIVES = re.compile(r"^\(\?:(.*)\)$", re.DOTALL)
_PIPE = re.compile(r"(?<!\\)\|")

# A pattern is checked against the text a caller wrote, but the service reads that text its own
# way: it may fold Unicode, decode percent-escapes, treat a backslash as a slash and resolve `..`.
# A value that only passes the pattern because of what it hides would then reach a place the owner
# did not allow (`.../azure/../dotnet/` passes `.../azure/.*` and the service fetches
# `.../dotnet/`).
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_DOT_SEGMENT = re.compile(r"(?:^|[/\\])(?:\.{2,}|\.(?=[;/\\?#]|$))")
_ENCODED_SEPARATOR = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)
_DECODE_ROUNDS = 3


def scalar_text(value: bool | str | int | float) -> str:
    """A value as it is compared: the text a caller wrote, with booleans as true and false."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return json.dumps(value)
    return str(value)


def values_pattern(values: Sequence[Scalar]) -> str:
    """The pattern that accepts exactly these values and nothing else."""
    return "(?:" + "|".join(re.escape(scalar_text(value)) for value in values) + ")"


def literal_values(pattern: str) -> list[str] | None:
    """The values a pattern accepts when it does nothing but list them, else None."""
    matched = _ALTERNATIVES.match(pattern)
    if not matched:
        return None
    literals: list[str] = []
    for alternative in _PIPE.split(matched.group(1)):
        plain = re.sub(r"\\(.)", r"\1", alternative)
        if not alternative or re.escape(plain) != alternative:
            return None
        literals.append(plain)
    return literals


def describe_limit(pattern: str) -> str:
    """A limit in words: the allowed values when it names them, the pattern otherwise."""
    literals = literal_values(pattern)
    if literals is None:
        return f"values matching {pattern}"
    return "one of " + ", ".join(literals) if len(literals) > 1 else literals[0]


def no_looser(old: str, new: str) -> bool:
    """Whether ``new`` accepts nothing ``old`` did not. Provable for the same pattern, or for
    lists of values where the new list is part of the old one; anything else counts as looser."""
    if new == old:
        return True
    before, after = literal_values(old), literal_values(new)
    return before is not None and after is not None and set(after) <= set(before)


def argument_texts(value: object) -> list[str] | None:
    """The texts to check for one argument, or None when it is not a value or a list of values."""
    if isinstance(value, bool | str | int | float):
        return [scalar_text(value)]
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        texts: list[str] = []
        for item in cast(Sequence[object], value):
            if not isinstance(item, bool | str | int | float):
                return None
            texts.append(scalar_text(item))
        return texts
    return None


def undeclared(schema: object, arguments: Mapping[str, object]) -> list[str]:
    """The arguments a call carries that the operation does not declare.

    A service may read an undeclared name as another way to say a limited argument, so an operation
    the owner has limited takes only what it declares, unless its schema says it takes more.
    """
    if not isinstance(schema, Mapping):
        return []
    document = cast(Mapping[str, object], schema)
    properties = document.get("properties")
    extra = document.get("additionalProperties")
    if not isinstance(properties, Mapping) or not properties or extra not in (None, False):
        return []
    declared = cast(Mapping[object, object], properties)
    return sorted(name for name in arguments if name not in declared)


def _readings(text: str) -> list[str]:
    """The ways a service may read one value: as written, with Unicode folded, and with each layer
    of percent-encoding removed."""
    folded = unicodedata.normalize("NFKC", text)
    readings = [text] if folded == text else [text, folded]
    current = folded
    for _ in range(_DECODE_ROUNDS):
        decoded = unquote(current)
        if decoded == current:
            break
        readings.append(decoded)
        current = decoded
    return readings


def smuggling(pattern: str, text: str) -> str | None:
    """What a value hides from a pattern that allows more than a fixed list, or None.

    A fixed list matches exactly, so it needs no check. For any other pattern a value may not carry
    control characters. For a pattern that names a place (it contains a slash) a value may also not
    carry a ``.`` or ``..`` path segment, a backslash or an encoded dot or slash, in any reading.
    """
    if literal_values(pattern) is not None:
        return None
    readings = _readings(text)
    if any(_CONTROL.search(reading) for reading in readings):
        return "control characters"
    if "/" not in pattern:
        return None
    if any("\\" in reading for reading in readings):
        return "a backslash"
    if any(_DOT_SEGMENT.search(reading) for reading in readings):
        return "a . or .. path segment"
    if any(_ENCODED_SEPARATOR.search(reading) for reading in readings):
        return "an encoded dot or slash"
    return None


def violation(patterns: Mapping[str, str], arguments: Mapping[str, object]) -> str | None:
    """Why a call breaks the owner's limits, or None when it keeps them."""
    for name, pattern in patterns.items():
        limit = describe_limit(pattern)
        if name not in arguments:
            return f"Argument {name} is required: the owner limits it to {limit}."
        texts = argument_texts(arguments[name])
        if not texts:
            return f"Argument {name} must be a value or a list of values, limited to {limit}."
        if any(re.fullmatch(pattern, text) is None for text in texts):
            return f"Argument {name} is limited by the owner to {limit}."
        for text in texts:
            hidden = smuggling(pattern, text)
            if hidden is not None:
                return (
                    f"Argument {name} is limited by the owner to {limit}, and this value carries "
                    f"{hidden}, which the service could read as another place."
                )
    return None
