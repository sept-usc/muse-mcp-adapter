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


PASSWORD_MARK = "pw1:"  # a salt that starts with this belongs to a password the owner chose
MIN_PASSWORD = 10
MAX_PASSWORD = 200


def check_password(password: str) -> str:
    """A password the owner chose: long enough, and compared exactly as typed."""
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"Use at least {MIN_PASSWORD} characters.")
    if len(password) > MAX_PASSWORD:
        raise ValueError(f"Use at most {MAX_PASSWORD} characters.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in password):
        raise ValueError("Use printable characters only.")
    return password


def new_password_salt() -> str:
    return PASSWORD_MARK + new_salt()


def hash_password(password: str, salt: str) -> str:
    return hashlib.scrypt(
        password.encode(),
        salt=bytes.fromhex(salt.removeprefix(PASSWORD_MARK)),
        n=2**15,
        r=8,
        p=1,
        dklen=32,
        maxmem=64 * 1024 * 1024,
    ).hex()


def matches(code: str, salt: str, expected: str) -> bool:
    """A password is compared exactly; a generated code ignores case, spaces and dashes."""
    if salt.startswith(PASSWORD_MARK):
        return hmac.compare_digest(hash_password(code, salt), expected)
    return hmac.compare_digest(hash_code(code, salt), expected)
