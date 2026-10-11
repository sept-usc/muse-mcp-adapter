"""Encrypting a backup you download: it holds the adapter's key file and tokens.

A passphrase becomes a key with scrypt, and the data is sealed with AES-GCM, which also detects any
change. File layout: 6-byte magic, 16-byte salt, 12-byte nonce, then the sealed data.
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"MMAVM1"
_SALT, _NONCE = 16, 12
_N, _R, _P = 2**15, 8, 1


def _key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=_N, r=_R, p=_P).derive(passphrase.encode())


def encrypt(data: bytes, passphrase: str) -> bytes:
    if len(passphrase) < 12:
        raise SystemExit("Use a backup passphrase of at least 12 characters.")
    salt, nonce = os.urandom(_SALT), os.urandom(_NONCE)
    return MAGIC + salt + nonce + AESGCM(_key(passphrase, salt)).encrypt(nonce, data, MAGIC)


def decrypt(blob: bytes, passphrase: str) -> bytes:
    header = len(MAGIC) + _SALT + _NONCE
    if not blob.startswith(MAGIC) or len(blob) < header + 16:
        raise SystemExit("This is not a muse-vm backup file.")
    salt = blob[len(MAGIC) : len(MAGIC) + _SALT]
    nonce = blob[len(MAGIC) + _SALT : header]
    try:
        return AESGCM(_key(passphrase, salt)).decrypt(nonce, blob[header:], MAGIC)
    except InvalidTag:
        raise SystemExit("Wrong passphrase, or the backup file was changed.") from None
