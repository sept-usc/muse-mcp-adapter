"""The owner's code: made once, typed on a review page, stored only as a salted hash.

It proves the person at the page is the owner. The agent never sees it: it is shown once on the
machine at enablement and typed only into the adapter's own pages.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to avoid misreading


def new_owner_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    return "-".join(raw[index : index + 4] for index in range(0, 12, 4))


def new_salt() -> str:
    return secrets.token_hex(16)


def normalize(code: str) -> str:
    return re.sub(r"[\s-]", "", code).upper()


def hash_code(code: str, salt: str) -> str:
    return hashlib.scrypt(
        normalize(code).encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32
    ).hex()


def matches(code: str, salt: str, expected: str) -> bool:
    return hmac.compare_digest(hash_code(code, salt), expected)
