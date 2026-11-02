"""Limits on the values a tool may be called with, chosen by the owner and enforced on every call.

A limit is a regular expression that the whole value of one argument must match (the same rule the
adapter already applies to resource template arguments). A limit that names allowed values is
stored as the pattern that accepts exactly those values, so there is one mechanism to enforce,
validate and show.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import cast

MAX_LIMITS_PER_TOOL = 10
Scalar = str | int | bool

_ALTERNATIVES = re.compile(r"^\(\?:(.*)\)$", re.DOTALL)
_PIPE = re.compile(r"(?<!\\)\|")


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
    return None
