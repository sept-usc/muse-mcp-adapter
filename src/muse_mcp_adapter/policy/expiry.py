"""When the owner's access to an operation ends, written in the published policy as ``expiresAt``.

Times are compared as instants, never as text. A value that cannot be read counts as already
ended, so a damaged policy fails closed instead of open.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime


def parse(value: object) -> datetime | None:
    """An ISO date-time in UTC (``...Z``) as an instant, or None if it is anything else."""
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return None
    return parsed.astimezone(UTC)


def ends_at(policy: Mapping[str, object]) -> str | None:
    """When access ends, as written in the policy, or None if it does not end."""
    value = policy.get("expiresAt")
    return value if isinstance(value, str) else None


def has_ended(policy: Mapping[str, object], now: str) -> bool:
    if policy.get("expiresAt") is None:
        return False
    ends, present = parse(policy.get("expiresAt")), parse(now)
    return ends is None or present is None or present >= ends


def no_later(old: object, new: object) -> bool:
    """Whether ``new`` ends no later than ``old``. Having an end is never later than having none."""
    if old is None:
        return True
    if new is None:
        return False
    before, after = parse(old), parse(new)
    return before is not None and after is not None and after <= before


def format_instant(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
