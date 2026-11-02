"""Encryption for the few secrets the adapter must hold for the owner.

A token the owner types on a review page, and the connector token shown once afterwards, are kept
only as ciphertext. The key is the one the adapter already keeps beside its database.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    @staticmethod
    def generate_key() -> bytes:
        return Fernet.generate_key()

    def seal(self, text: str) -> str:
        return self._fernet.encrypt(text.encode()).decode()

    def open(self, sealed: str) -> str | None:
        try:
            return self._fernet.decrypt(sealed.encode()).decode()
        except InvalidToken:
            return None


class ChainedSecrets(Mapping[str, str]):
    """The usual secret lookup, then the secrets the owner typed on a review page."""

    def __init__(self, primary: Mapping[str, str], fallback: Callable[[str], str | None]) -> None:
        self._primary = primary
        self._fallback = fallback

    def __getitem__(self, name: str) -> str:
        value = self._primary.get(name)
        if value:
            return value
        stored = self._fallback(name)
        if stored is None:
            raise KeyError(name)
        return stored

    def __iter__(self) -> Iterator[str]:
        return iter(self._primary)

    def __len__(self) -> int:
        return len(self._primary)
