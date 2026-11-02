"""Constants, proposal states, and the request an agent may make. Imports nothing heavy."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from ..domain.errors import RequestValidationError
from ..domain.models import DataPlaneRequest, DataPlaneResponse

SETUP_SERVER_ID = "adapter-setup"
# The setup server is built in and never contacted. The address only has to look like one, and
# ``.invalid`` can never resolve, so nothing can ever be reached through it.
SETUP_ENDPOINT = "https://setup.invalid/builtin"
SETUP_LABEL = "Adapter setup"

ACTOR_AGENT = "chat-setup:agent"
ACTOR_OWNER = "chat-setup:owner"
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

_SERVER = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
_CONNECTION = re.compile(r"^conn_[0-9a-f]{12}$")
_NAME = re.compile(r"^[A-Za-z0-9_.:\-]{1,120}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_CONNECT_FIELDS = (
    "server",
    "endpoint",
    "userWords",
    "readOnly",
    "alsoAllow",
    "exclude",
    "timeoutSeconds",
    "maxResultBytes",
    "notes",
)
_CHANGE_FIELDS = (
    "connectionId",
    *(item for item in _CONNECT_FIELDS if item not in ("server", "endpoint")),
)


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
    also_allow: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    timeout_seconds: int | None = None
    max_result_bytes: int | None = None
    notes: str | None = None

    def to_json(self) -> dict[str, object]:
        """Only the fields that are set, so a stored request parses back the way it came."""
        payload: dict[str, object] = {
            "userWords": self.user_words,
            "server": self.server,
            "endpoint": self.endpoint,
            "connectionId": self.connection_id,
            "readOnly": self.read_only,
            "alsoAllow": list(self.also_allow),
            "exclude": list(self.exclude),
            "timeoutSeconds": self.timeout_seconds,
            "maxResultBytes": self.max_result_bytes,
            "notes": self.notes,
        }
        return {key: value for key, value in payload.items() if value is not None}

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
            also_allow=_names(value.get("alsoAllow"), "alsoAllow"),
            exclude=_names(value.get("exclude"), "exclude"),
            timeout_seconds=_integer(value.get("timeoutSeconds"), "timeoutSeconds", 1, 300),
            max_result_bytes=_integer(
                value.get("maxResultBytes"), "maxResultBytes", 1024, 16 * 1024 * 1024
            ),
            notes=_text(value.get("notes"), "notes", MAX_NOTES, required=False),
        )


def _text(value: object, field: str, limit: int, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise RequestValidationError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise RequestValidationError(f"{field} must be text")
    cleaned = _CONTROL.sub(" ", value).strip()
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
        or any(character.isspace() or ord(character) < 32 for character in value)
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
