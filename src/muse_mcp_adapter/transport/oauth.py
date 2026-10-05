from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from cryptography.fernet import Fernet, InvalidToken
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)
from pydantic import AnyUrl

from ..catalog.canonical import digest
from ..domain.models import ResolvedCredential


class OAuthStorageBackend(Protocol):
    async def get(self, namespace: str, key: str) -> object | None: ...

    async def set(self, namespace: str, key: str, value: object) -> None: ...


class InMemoryOAuthStorageBackend:
    """Test/default storage; production deployments inject their secret store."""

    def __init__(self) -> None:
        self._values: dict[tuple[str, str], object] = {}

    async def get(self, namespace: str, key: str) -> object | None:
        return self._values.get((namespace, key))

    async def set(self, namespace: str, key: str, value: object) -> None:
        self._values[(namespace, key)] = value


class EncryptedFileOAuthStorageBackend:
    """Persists OAuth state encrypted at rest (Fernet) in an owner-only file."""

    def __init__(self, path: Path, key: bytes) -> None:
        self._path = path
        self._fernet = Fernet(key)

    @staticmethod
    def generate_key() -> bytes:
        return Fernet.generate_key()

    def _load(self) -> dict[str, dict[str, object]]:
        if not self._path.exists():
            return {}
        try:
            raw = self._fernet.decrypt(self._path.read_bytes())
        except InvalidToken as error:
            raise ValueError("OAuth storage cannot be decrypted with the configured key") from error
        loaded: object = json.loads(raw)
        return loaded if isinstance(loaded, dict) else {}  # type: ignore[return-value]

    def _save(self, state: dict[str, dict[str, object]]) -> None:
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(self._fernet.encrypt(json.dumps(state).encode()))
        os.replace(temporary, self._path)

    async def get(self, namespace: str, key: str) -> object | None:
        value = self._load().get(namespace, {}).get(key)
        if value is None:
            return None
        if key == "tokens":
            return OAuthToken.model_validate(value)
        if key == "client_info":
            return OAuthClientInformationFull.model_validate(value)
        return None

    async def set(self, namespace: str, key: str, value: object) -> None:
        state = self._load()
        dump = getattr(value, "model_dump", None)
        if dump is None:
            raise TypeError("Only OAuth models can be stored")
        state.setdefault(namespace, {})[key] = dump(mode="json", exclude_none=True)
        self._save(state)


class NamespacedTokenStorage(TokenStorage):
    """Keeps OAuth tokens and dynamic client registrations connection-scoped.

    Tokens that do not cover the required scopes are withheld so the SDK re-runs authorization
    with the requested scopes instead of silently using an under-privileged or stale grant.
    """

    def __init__(
        self,
        backend: OAuthStorageBackend,
        namespace: str,
        required_scopes: frozenset[str] = frozenset(),
    ) -> None:
        if not namespace:
            raise ValueError("OAuth storage namespace is required")
        self._backend = backend
        self._namespace = namespace
        self._required_scopes = required_scopes

    async def get_tokens(self) -> OAuthToken | None:
        value = await self._backend.get(self._namespace, "tokens")
        if not isinstance(value, OAuthToken):
            return None
        if value.scope is not None and not self._required_scopes <= set(value.scope.split()):
            return None
        return value

    async def set_tokens(self, tokens: OAuthToken) -> None:
        await self._backend.set(self._namespace, "tokens", tokens)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        value = await self._backend.get(self._namespace, "client_info")
        return value if isinstance(value, OAuthClientInformationFull) else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        await self._backend.set(self._namespace, "client_info", client_info)


@dataclass(frozen=True, slots=True)
class OAuthClientConfig:
    redirect_uri: str
    client_name: str = "Muse MCP Adapter"
    scopes: tuple[str, ...] = ()
    client_metadata_url: str | None = None
    authorization_timeout_seconds: float = 300.0


class OAuthAuthFactory:
    """Builds the SDK's OAuth/PKCE provider for one isolated connection."""

    def __init__(
        self,
        *,
        config: OAuthClientConfig,
        backend: OAuthStorageBackend,
        redirect_handler: Callable[[str], Awaitable[None]],
        callback_handler: Callable[[], Awaitable[tuple[str, str | None]]],
    ) -> None:
        self._config = config
        self._backend = backend
        self._redirect_handler = redirect_handler
        self._callback_handler = callback_handler

    def __call__(
        self, endpoint: str, credential: ResolvedCredential | None
    ) -> httpx.Auth:
        if credential is None:
            raise ValueError("OAuth requires a connection-scoped credential reference")
        if not (
            credential.local_identity_id
            and credential.connection_id
            and credential.server_id
        ):
            raise ValueError(
                "OAuth credentials must include identity, connection, and server bindings"
            )
        namespace = digest(
            {
                "endpoint": endpoint,
                "credentialRef": credential.credential_ref,
                "localIdentityId": credential.local_identity_id,
                "connectionId": credential.connection_id,
                "serverId": credential.server_id,
            }
        )
        scopes = tuple(sorted({*self._config.scopes, *credential.scopes}))
        metadata = OAuthClientMetadata(
            redirect_uris=[AnyUrl(self._config.redirect_uri)],
            client_name=self._config.client_name,
            scope=" ".join(scopes) or None,
        )
        return OAuthClientProvider(
            endpoint,
            metadata,
            NamespacedTokenStorage(self._backend, namespace, frozenset(scopes)),
            redirect_handler=self._redirect_handler,
            callback_handler=self._callback_handler,
            timeout=self._config.authorization_timeout_seconds,
            client_metadata_url=self._config.client_metadata_url,
        )
