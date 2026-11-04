"""Constants, proposal states, and the request an agent may make. Imports nothing heavy."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from ..domain.errors import RequestValidationError
from ..domain.models import DataPlaneRequest, DataPlaneResponse
from ..policy.argument_limits import scalar_text, values_pattern
from ..policy.validate_policy import regex_problems

SETUP_SERVER_ID = "adapter-setup"
# The setup server is built in and never contacted. The address only has to look like one, and
# ``.invalid`` can never resolve, so nothing can ever be reached through it.
SETUP_ENDPOINT = "https://setup.invalid/builtin"
SETUP_LABEL = "Adapter setup"

ACTOR_AGENT = "chat-setup:agent"
ACTOR_OWNER = "chat-setup:owner"
ACTOR_VISITOR = (
    "chat-setup:unverified"  # someone at a review page who has not proved to be the owner
)
ACTOR_SYSTEM = "chat-setup"

KIND_CONNECT = "connect"
KIND_CHANGE = "change"

WAITING = "waiting_for_owner"
CONNECTING = "connecting"
IN_REVIEW = "in_review"
APPROVED = "approved"
APPLIED = "applied"
DECLINED = "declined"
EXPIRED = "expired"
FAILED = "failed"
OPEN_STATES = (WAITING, CONNECTING, IN_REVIEW)

MAX_WORDS = 2000
MAX_NOTES = 1000
MAX_NAMES = 50
MAX_ENDPOINT = 500

MAX_ACCESS_HOURS = 720  # thirty days: longer than that is not "temporary"
MAX_LIMITS = 20
MAX_VALUES = 20
MAX_VALUE_CHARS = 200
_ARGUMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_LIMIT_FIELDS = ("operation", "argument", "allowedValues", "pattern")
_SERVER = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
_CONNECTION = re.compile(r"^conn_[0-9a-f]{12}$")
_NAME = re.compile(r"^[A-Za-z0-9_.:\-]{1,120}$")
_INVISIBLE = {"Cc", "Cf", "Zl", "Zp"}  # controls, format characters (bidi, zero-width), separators


def _readable(value: str) -> str:
    """Text as the owner will read it: controls other than tab and line breaks become spaces and
    format characters (a right-to-left override, zero-width marks) disappear, so that what is
    shown is what is stored."""
    kept: list[str] = []
    for character in value:
        category = unicodedata.category(character)
        if character in "\t\n\r":
            kept.append(character)
        elif category in {"Cc", "Zl", "Zp"}:
            kept.append(" ")
        elif category != "Cf":
            kept.append(character)
    return "".join(kept)


def _hidden(value: str) -> bool:
    return any(unicodedata.category(character) in _INVISIBLE for character in value)


_CONNECT_FIELDS = (
    "server",
    "endpoint",
    "userWords",
    "readOnly",
    "includeReads",
    "alsoAllow",
    "exclude",
    "reasons",
    "timeoutSeconds",
    "maxResultBytes",
    "expiresInHours",
    "limitArguments",
    "notes",
)
REVISE_FIELDS = (
    "readOnly",
    "includeReads",
    "alsoAllow",
    "exclude",
    "reasons",
    "timeoutSeconds",
    "maxResultBytes",
    "expiresInHours",
    "limitArguments",
    "notes",
)
_CHANGE_FIELDS = (
    "connectionId",
    *(item for item in _CONNECT_FIELDS if item not in ("server", "endpoint")),
)


@dataclass(frozen=True, slots=True)
class ArgumentLimit:
    """A limit the user asked for on what an operation may be called with."""

    operation: str  # an operation name, or * for every operation that has the argument
    argument: str
    values: tuple[str | int | bool, ...] = ()
    pattern: str | None = None

    def regex(self) -> str:
        return self.pattern if self.pattern is not None else values_pattern(self.values)

    def to_json(self) -> dict[str, object]:
        body: dict[str, object] = {"operation": self.operation, "argument": self.argument}
        if self.pattern is not None:
            body["pattern"] = self.pattern
        else:
            body["allowedValues"] = list(self.values)
        return body

    def describe(self) -> str:
        shown = (
            self.pattern
            if self.pattern is not None
            else ", ".join(scalar_text(value) for value in self.values)
        )
        return f"{self.argument} of {self.operation}: {shown}"


def parse_limits(value: object, field: str = "limitArguments") -> tuple[ArgumentLimit, ...]:
    """Check limits an agent asked for; each names an argument and either values or a pattern."""
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise RequestValidationError(f"{field} must be a list")
    items = cast(Sequence[object], value)
    if len(items) > MAX_LIMITS:
        raise RequestValidationError(f"{field} may hold at most {MAX_LIMITS} limits")
    limits: list[ArgumentLimit] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise RequestValidationError(f"{field} holds something that is not a limit")
        body = cast(Mapping[str, object], item)
        unknown = sorted(str(key) for key in body if key not in _LIMIT_FIELDS)
        if unknown:
            raise RequestValidationError(f"A limit does not accept: {', '.join(unknown)}")
        operation, argument = body.get("operation"), body.get("argument")
        if not isinstance(operation, str) or not (operation == "*" or _NAME.fullmatch(operation)):
            raise RequestValidationError("A limit needs the name of an operation, or *")
        if not isinstance(argument, str) or not _ARGUMENT.fullmatch(argument):
            raise RequestValidationError("A limit needs the name of an argument")
        allowed, pattern = body.get("allowedValues"), body.get("pattern")
        if (allowed is None) == (pattern is None):
            raise RequestValidationError("Give a limit either allowedValues or a pattern")
        if pattern is not None:
            if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_VALUE_CHARS:
                raise RequestValidationError("A pattern must be short text")
            if _hidden(pattern):
                raise RequestValidationError(
                    "A pattern must not hold control or invisible characters"
                )
            problems = regex_problems(pattern)
            if problems:
                raise RequestValidationError(problems[0])
            limits.append(ArgumentLimit(operation, argument, pattern=pattern))
            continue
        values = _values(allowed)
        limits.append(ArgumentLimit(operation, argument, values=values))
    return tuple(limits)


def _values(value: object) -> tuple[str | int | bool, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise RequestValidationError("allowedValues must be a list")
    items = cast(Sequence[object], value)
    if not 1 <= len(items) <= MAX_VALUES:
        raise RequestValidationError(f"allowedValues holds 1 to {MAX_VALUES} values")
    values: list[str | int | bool] = []
    for item in items:
        if isinstance(item, str):
            if not item or len(item) > MAX_VALUE_CHARS:
                raise RequestValidationError("An allowed value must be short, non-empty text")
            if _hidden(item):
                raise RequestValidationError(
                    "An allowed value must not hold control or invisible characters"
                )
        elif not isinstance(item, int):
            raise RequestValidationError("An allowed value is text, a whole number or true/false")
        values.append(item)
    return tuple(values)


class SetupExecutor(Protocol):
    """What the runtime calls for operations of the built-in setup server."""

    def execute(self, request: DataPlaneRequest) -> DataPlaneResponse: ...


@dataclass(frozen=True, slots=True)
class SetupRequest:
    """What the user asked for, as the agent relays it. Never trusted; always shown for review."""

    user_words: str
    server: str | None = None
    endpoint: str | None = None
    connection_id: str | None = None
    read_only: bool = False
    include_reads: bool = False  # every read operation, plus whatever else is asked for
    reasons: tuple[tuple[str, str], ...] = ()  # (operation name, why Muse wants it), sorted
    also_allow: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    timeout_seconds: int | None = None
    max_result_bytes: int | None = None
    expires_in_hours: int | None = None  # access ends this long after the owner approves
    limits: tuple[ArgumentLimit, ...] = ()
    notes: str | None = None

    def to_json(self) -> dict[str, object]:
        """Only the fields that are set, so a stored request parses back the way it came."""
        payload: dict[str, object] = {
            "userWords": self.user_words,
            "server": self.server,
            "endpoint": self.endpoint,
            "connectionId": self.connection_id,
            "readOnly": self.read_only,
            "includeReads": self.include_reads,
            "reasons": dict(self.reasons),
            "alsoAllow": list(self.also_allow),
            "exclude": list(self.exclude),
            "timeoutSeconds": self.timeout_seconds,
            "maxResultBytes": self.max_result_bytes,
            "expiresInHours": self.expires_in_hours,
            "limitArguments": [limit.to_json() for limit in self.limits],
            "notes": self.notes,
        }
        return {
            key: value
            for key, value in payload.items()
            if value is not None and value != [] or key in ("alsoAllow", "exclude")
        }

    @classmethod
    def parse(cls, value: Mapping[str, object], *, change: bool) -> SetupRequest:
        """Check an agent's arguments strictly. Unknown fields are refused, never ignored."""
        allowed = _CHANGE_FIELDS if change else _CONNECT_FIELDS
        unknown = sorted(str(key) for key in value if key not in allowed)
        if unknown:
            raise RequestValidationError(
                "These fields are not accepted: "
                + ", ".join(unknown)
                + ". Accepted: "
                + ", ".join(allowed)
                + ". Secrets are never accepted here."
            )
        words = _text(value.get("userWords"), "userWords", MAX_WORDS, required=True)
        server = _optional_match(value.get("server"), "server", _SERVER)
        endpoint = _endpoint(value.get("endpoint"))
        connection_id = _optional_match(value.get("connectionId"), "connectionId", _CONNECTION)
        if change and connection_id is None:
            raise RequestValidationError("connectionId is required")
        if not change and (server is None) == (endpoint is None):
            raise RequestValidationError("Give either server or endpoint, not both and not neither")
        return cls(
            user_words=cast(str, words),
            server=server,
            endpoint=endpoint,
            connection_id=connection_id,
            read_only=_boolean(value.get("readOnly"), "readOnly"),
            include_reads=_boolean(value.get("includeReads"), "includeReads"),
            reasons=_reasons(value.get("reasons")),
            also_allow=_names(value.get("alsoAllow"), "alsoAllow"),
            exclude=_names(value.get("exclude"), "exclude"),
            timeout_seconds=_integer(value.get("timeoutSeconds"), "timeoutSeconds", 1, 300),
            max_result_bytes=_integer(
                value.get("maxResultBytes"), "maxResultBytes", 1024, 16 * 1024 * 1024
            ),
            expires_in_hours=_integer(
                value.get("expiresInHours"), "expiresInHours", 1, MAX_ACCESS_HOURS
            ),
            limits=parse_limits(value.get("limitArguments")),
            notes=_text(value.get("notes"), "notes", MAX_NOTES, required=False),
        )


def _text(value: object, field: str, limit: int, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise RequestValidationError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise RequestValidationError(f"{field} must be text")
    cleaned = _readable(value).strip()
    if not cleaned:
        if required:
            raise RequestValidationError(f"{field} must not be empty")
        return None
    if len(cleaned) > limit:
        raise RequestValidationError(f"{field} is longer than {limit} characters")
    return cleaned


def _optional_match(value: object, field: str, pattern: re.Pattern[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise RequestValidationError(f"{field} is not in the expected form")
    return value


def _endpoint(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) > MAX_ENDPOINT
        or not value.startswith(("https://", "http://"))
        or any(character.isspace() for character in value)
        or _hidden(value)
    ):
        raise RequestValidationError("endpoint must be the https address of an MCP server")
    return value


def _boolean(value: object, field: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise RequestValidationError(f"{field} must be true or false")
    return value


def _integer(value: object, field: str, low: int, high: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise RequestValidationError(f"{field} must be a whole number from {low} to {high}")
    return value


MAX_REASONS = 50
MAX_REASON = 200


def _reasons(value: object) -> tuple[tuple[str, str], ...]:
    """Why Muse wants each operation, a few words each. Shown to the owner as Muse's, unchecked."""
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise RequestValidationError("reasons must map an operation name to a short reason")
    items = cast(Mapping[object, object], value)
    if len(items) > MAX_REASONS:
        raise RequestValidationError(f"reasons holds more than {MAX_REASONS} operations")
    found: dict[str, str] = {}
    for name, why in items.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise RequestValidationError("reasons holds a name that is not in the expected form")
        text = _text(why, f"the reason for {name}", MAX_REASON, required=True)
        found[name] = cast(str, text)
    return tuple(sorted(found.items()))


def _names(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise RequestValidationError(f"{field} must be a list of names")
    items = cast(Sequence[object], value)
    if len(items) > MAX_NAMES:
        raise RequestValidationError(f"{field} may hold at most {MAX_NAMES} names")
    names: list[str] = []
    for item in items:
        if not isinstance(item, str) or not _NAME.fullmatch(item):
            raise RequestValidationError(f"{field} holds a name that is not in the expected form")
        if item not in names:
            names.append(item)
    return tuple(names)
