from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from ..catalog.canonical import canonical_json
from ..domain.models import ApprovalEvidence

DEFAULT_MAX_TTL_SECONDS: Mapping[str, int] = {"write": 900, "sensitive_write": 300}
_MIN_KEY_BYTES = 32


class ApprovalVerifier(Protocol):
    def verify(
        self,
        evidence: ApprovalEvidence,
        *,
        classification: str,
        action_digest: str,
        now: str,
    ) -> str | None:
        """Return None when the evidence is valid, otherwise a human-readable reason."""
        ...


class HmacApprovalVerifier:
    """Verifies approval evidence minted by the Muse-side approval authority.

    The authority and the adapter share a secret; evidence is only valid if its HMAC covers the
    approval ID, classification, exact action digest and validity window. Callers who can read an
    action digest therefore cannot mint evidence for it.
    """

    def __init__(
        self,
        secret: bytes,
        *,
        max_ttl_seconds: Mapping[str, int] = DEFAULT_MAX_TTL_SECONDS,
    ) -> None:
        if len(secret) < _MIN_KEY_BYTES:
            raise ValueError(f"Approval secret must be at least {_MIN_KEY_BYTES} bytes")
        self._secret = secret
        self._max_ttl = dict(max_ttl_seconds)

    def issue(
        self,
        *,
        approval_id: str,
        classification: str,
        action_digest: str,
        approved_at: str,
        expires_at: str,
    ) -> ApprovalEvidence:
        return ApprovalEvidence(
            approval_id=approval_id,
            classification=classification,
            action_digest=action_digest,
            approved_at=approved_at,
            expires_at=expires_at,
            signature=self._signature(
                approval_id, classification, action_digest, approved_at, expires_at
            ),
        )

    def verify(
        self,
        evidence: ApprovalEvidence,
        *,
        classification: str,
        action_digest: str,
        now: str,
    ) -> str | None:
        if not evidence.approval_id:
            return "Approval evidence has no approval ID"
        expected = self._signature(
            evidence.approval_id,
            evidence.classification,
            evidence.action_digest,
            evidence.approved_at,
            evidence.expires_at,
        )
        if not evidence.signature or not hmac.compare_digest(evidence.signature, expected):
            return "Approval evidence signature is invalid"
        if evidence.classification != classification:
            return "Approval classification does not match the published operation"
        if evidence.action_digest != action_digest:
            return "Approval evidence does not match the exact requested action"
        try:
            approved_at = _time(evidence.approved_at)
            expires_at = _time(evidence.expires_at)
            current = _time(now)
        except ValueError:
            return "Approval evidence timestamps are invalid"
        if not approved_at <= current <= expires_at:
            return "Approval evidence is not currently valid"
        max_ttl = self._max_ttl.get(classification)
        if max_ttl is None or (expires_at - approved_at).total_seconds() > max_ttl:
            return "Approval evidence validity window exceeds the allowed maximum"
        return None

    def _signature(
        self,
        approval_id: str,
        classification: str,
        action_digest: str,
        approved_at: str,
        expires_at: str,
    ) -> str:
        payload = canonical_json(
            {
                "approvalId": approval_id,
                "classification": classification,
                "actionDigest": action_digest,
                "approvedAt": approved_at,
                "expiresAt": expires_at,
            }
        )
        return hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must have a timezone")
    return parsed
