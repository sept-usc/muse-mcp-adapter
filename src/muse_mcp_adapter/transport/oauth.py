from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from cryptography.fernet import Fernet, InvalidToken
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
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
        self._lock = threading.Lock()

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
        with self._lock:
            value = self._load().get(namespace, {}).get(key)
        if value is None:
            return None
        if key == "tokens":
            return OAuthToken.model_validate(value)
        if key == "client_info":
            return OAuthClientInformationFull.model_validate(value)
        if key == "state" and isinstance(value, dict):
            return cast(dict[str, object], value)
        return None

    async def set(self, namespace: str, key: str, value: object) -> None:
        dump = getattr(value, "model_dump", None)
        if dump is not None:
            stored: object = dump(mode="json", exclude_none=True)
        elif key == "state" and isinstance(value, dict):
            stored = cast(dict[str, object], value)
        else:
            raise TypeError("Only OAuth models and plain state can be stored")
        with self._lock:
            state = self._load()
            state.setdefault(namespace, {})[key] = stored
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
        # Remember when the access token expires so a later provider refreshes it in time.
        expires_at = (
            time.time() + max(tokens.expires_in - _EXPIRY_SKEW_SECONDS, 0)
            if tokens.expires_in
            else None
        )
        await self.update_state(expiresAt=expires_at)

    async def get_state(self) -> dict[str, object]:
        value = await self._backend.get(self._namespace, "state")
        return cast(dict[str, object], value) if isinstance(value, dict) else {}

    async def update_state(self, **values: object) -> None:
        state = await self.get_state()
        for key, value in values.items():
            if value is None:
                state.pop(key, None)
            else:
                state[key] = value
        await self._backend.set(self._namespace, "state", state)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        value = await self._backend.get(self._namespace, "client_info")
        return value if isinstance(value, OAuthClientInformationFull) else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        await self._backend.set(self._namespace, "client_info", client_info)


_EXPIRY_SKEW_SECONDS = 30


class AdapterOAuthProvider(OAuthClientProvider):
    """The SDK's OAuth client, primed with what an earlier sign-in learned.

    A new provider is created for every downstream call. Without help it would not know when
    the stored access token expires, nor where the provider's token endpoint is, so it would send
    an expired token and fall back to a full sign-in instead of quietly using the refresh token.
    """

    def __init__(self, *args: Any, adapter_storage: NamespacedTokenStorage, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._adapter_storage = adapter_storage

    async def _initialize(self) -> None:
        await super()._initialize()
        state = await self._adapter_storage.get_state()
        expires_at = state.get("expiresAt")
        if isinstance(expires_at, int | float):
            self.context.token_expiry_time = float(expires_at)
        server = state.get("authServerUrl")
        if isinstance(server, str):
            self.context.auth_server_url = server
        try:
            metadata = state.get("oauthMetadata")
            if isinstance(metadata, dict):
                self.context.oauth_metadata = OAuthMetadata.model_validate(metadata)
            resource = state.get("protectedResource")
            if isinstance(resource, dict):
                self.context.protected_resource_metadata = ProtectedResourceMetadata.model_validate(
                    resource
                )
        except ValueError:
            pass  # stale or unreadable discovery data is simply rediscovered when needed

    async def persist_discovery(self) -> None:
        """Save the provider metadata learned during sign-in for later, headless refreshes."""
        context = self.context
        await self._adapter_storage.update_state(
            authServerUrl=context.auth_server_url,
            oauthMetadata=(
                context.oauth_metadata.model_dump(mode="json", exclude_none=True)
                if context.oauth_metadata
                else None
            ),
            protectedResource=(
                context.protected_resource_metadata.model_dump(mode="json", exclude_none=True)
                if context.protected_resource_metadata
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class OAuthClientConfig:
    redirect_uri: str
    client_name: str = "Muse MCP Adapter"
    scopes: tuple[str, ...] = ()
    client_metadata_url: str | None = None
    authorization_timeout_seconds: float = 300.0


def namespace_for(endpoint: str, credential: ResolvedCredential) -> str:
    """Storage namespace bound to one endpoint, identity, connection and server."""
    return digest(
        {
            "endpoint": endpoint,
            "credentialRef": credential.credential_ref,
            "localIdentityId": credential.local_identity_id,
            "connectionId": credential.connection_id,
            "serverId": credential.server_id,
        }
    )


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
    ) -> AdapterOAuthProvider:
        if credential is None:
            raise ValueError("OAuth requires a connection-scoped credential reference")
        if not (credential.local_identity_id and credential.connection_id and credential.server_id):
            raise ValueError(
                "OAuth credentials must include identity, connection, and server bindings"
            )
        namespace = namespace_for(endpoint, credential)
        scopes = tuple(sorted({*self._config.scopes, *credential.scopes}))
        metadata = OAuthClientMetadata(
            redirect_uris=[AnyUrl(self._config.redirect_uri)],
            client_name=self._config.client_name,
            scope=" ".join(scopes) or None,
        )
        storage = NamespacedTokenStorage(self._backend, namespace, frozenset(scopes))
        return AdapterOAuthProvider(
            endpoint,
            metadata,
            storage,
            adapter_storage=storage,
            redirect_handler=self._redirect_handler,
            callback_handler=self._callback_handler,
            timeout=self._config.authorization_timeout_seconds,
            client_metadata_url=self._config.client_metadata_url,
        )
