from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import cast

from ..persistence.sqlite_repository import SqliteControlPlaneRepository

_SENSITIVE_FRAGMENTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "apikey",
    "credential",
    "cookie",
    "signature",
    "privatekey",
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_KEY_VALUE_SECRET = re.compile(
    r"(?i)\b(access_token|refresh_token|api[_-]?key|client_secret|password)=([^&\s]+)"
)


class AuditRecorder:
    def __init__(self, repository: SqliteControlPlaneRepository) -> None:
        self._repository = repository

    def record(
        self,
        operation_id: str,
        *,
        event_type: str,
        occurred_at: str,
        data: Mapping[str, object],
    ) -> None:
        redacted = _redact(data)
        self._repository.append_operation_event(
            operation_id,
            event_type=event_type,
            occurred_at=occurred_at,
            data=cast(Mapping[str, object], redacted),
        )


def _sensitive_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return any(fragment in normalized for fragment in _SENSITIVE_FRAGMENTS)


def _redact(value: object) -> object:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {
            str(key): "[REDACTED]" if _sensitive_key(key) else _redact(item)
            for key, item in mapping.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_redact(item) for item in cast(Sequence[object], value)]
    if isinstance(value, str):
        return _KEY_VALUE_SECRET.sub(r"\1=[REDACTED]", _BEARER.sub(r"\1 [REDACTED]", value))
    return value
