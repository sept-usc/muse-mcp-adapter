"""Browser-facing delegated OAuth for the owner's own account at an MCP provider.

The adapter often runs on a remote machine, so the provider must redirect the owner's
browser to a public callback served by the adapter itself. The flow:

1. A one-time link (``/connect/<nonce>``) is shown to the owner, by the CLI or inside a
   ``REAUTHORIZATION_REQUIRED`` error that Muse relays in chat.
2. Opening it binds the flow to that browser with a cookie and sends the browser on to the
   provider's consent screen.
3. The provider redirects to ``/oauth/callback``. The adapter checks the cookie and the
   state, completes the token exchange with its own PKCE verifier, and stores the tokens
   encrypted. Muse never sees them.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from ..data_plane.tool_client import McpReauthorizationRequired
from ..domain.errors import EntityNotFoundError
from ..domain.models import ConnectionRecord, ResolvedCredential
from ..persistence.sqlite_repository import SqliteControlPlaneRepository
from ..transport.oauth import (
    AdapterOAuthProvider,
    NamespacedTokenStorage,
    OAuthAuthFactory,
    OAuthClientConfig,
    OAuthStorageBackend,
    namespace_for,
)
from ..transport.sdk_client import StreamableHttpSessionFactory
from ..transport.security import EndpointSecurityPolicy
from .web_pages import page, redirect

COOKIE_NAME = "muse_adapter_login"
CALLBACK_PATH = "/oauth/callback"


@dataclass(frozen=True, slots=True)
class OAuthClientSeed:
    """A pre-registered OAuth client, for providers without dynamic client registration."""

    client_id: str
    client_secret: str | None = None


@dataclass(slots=True)
class _Pending:
    connection: ConnectionRecord
    cookie_secret: str
    started_at: datetime
    url_future: asyncio.Future[str] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )
    code_future: asyncio.Future[tuple[str, str | None]] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )
    task: asyncio.Task[None] | None = None
    state: str | None = None
    continue_to: str | None = None


class OAuthWebFlow:
    def __init__(
        self,
        *,
        repository: SqliteControlPlaneRepository,
        public_url: str,
        backend: OAuthStorageBackend,
        endpoint_policy: EndpointSecurityPolicy,
        clock: Callable[[], str],
        client_name: str = "Muse MCP Adapter",
        pending_ttl_seconds: int = 600,
        nonce_ttl_seconds: int = 900,
        clients: Mapping[str, OAuthClientSeed] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._repository = repository
        self._public_url = public_url.rstrip("/")
        self._backend = backend
        self._policy = endpoint_policy
        self._clock = clock
        self._client_name = client_name
        self._ttl = pending_ttl_seconds
        self._nonce_ttl = nonce_ttl_seconds
        self._clients = dict(clients or {})
        self._transport = transport
        self._pending: dict[str, _Pending] = {}

    @property
    def callback_url(self) -> str:
        return f"{self._public_url}{CALLBACK_PATH}"

    def login_link(self, connection_id: str, continue_to: str | None = None) -> str:
        """Create a one-time sign-in link for a connection (callable from any thread).

        ``continue_to`` is a path on this service the browser is sent to once the provider
        sign-in succeeds, which lets another flow (such as linking Muse) resume afterwards.
        """
        nonce = secrets.token_urlsafe(24)
        now = _parse(self._clock())
        self._repository.create_login_nonce(
            _hash(nonce),
            connection_id,
            now.isoformat().replace("+00:00", "Z"),
            (now + timedelta(seconds=self._nonce_ttl)).isoformat().replace("+00:00", "Z"),
            continue_to,
        )
        return f"{self._public_url}/connect/{nonce}"

    def routes(self) -> list[Route]:
        return [
            Route("/connect/{nonce}", self._connect, methods=["GET"]),
            Route(CALLBACK_PATH, self._callback, methods=["GET"]),
        ]

    # --- start -------------------------------------------------------------------------

    async def _connect(self, request: Request) -> Response:
        self._expire()
        try:
            connection_id, continue_to = await anyio.to_thread.run_sync(
                lambda: self._repository.consume_login_nonce(
                    _hash(request.path_params["nonce"]), self._clock()
                )
            )
            connection = await anyio.to_thread.run_sync(
                lambda: self._repository.get_connection(connection_id)
            )
        except EntityNotFoundError:
            return page("Link not valid", "This sign-in link is invalid, expired, or used.", 404)
        if connection.revoked_at is not None or connection.credential_kind != "oauth":
            return page("Link not valid", "This connection cannot be signed in.", 404)

        pending = _Pending(
            connection=connection,
            cookie_secret=secrets.token_urlsafe(24),
            started_at=datetime.now(UTC),
            continue_to=continue_to if _safe_path(continue_to) else None,
        )
        pending.task = asyncio.create_task(self._run_login(pending))
        done, _ = await asyncio.wait(
            {pending.url_future, pending.task},
            timeout=30,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if pending.url_future in done:
            url = pending.url_future.result()
            response: Response = RedirectResponse(url, status_code=302)
            response.set_cookie(
                COOKIE_NAME,
                pending.cookie_secret,
                max_age=self._ttl,
                httponly=True,
                secure=self._public_url.startswith("https://"),
                samesite="lax",
                path="/",
            )
            response.headers["Cache-Control"] = "no-store"
            return response
        if pending.task in done and pending.task.exception() is None:
            return page("Already connected", "This account is already connected. Return to Muse.")
        self._discard(pending)
        return page(
            "Could not start sign-in",
            "The service did not offer a sign-in. Check the connection and try again.",
            502,
        )

    async def _run_login(self, pending: _Pending) -> None:
        connection = pending.connection
        server = await anyio.to_thread.run_sync(
            lambda: self._repository.get_server(connection.server_id)
        )
        reference = f"connection:{connection.connection_id}"
        credential = ResolvedCredential(
            credential_ref=reference,
            access_token="",
            scopes=frozenset(connection.scopes),
            local_identity_id=connection.identity_id,
            connection_id=connection.connection_id,
            server_id=connection.server_id,
        )
        await self._seed_client(server.server_id, namespace_for(server.endpoint, credential))

        async def redirect(url: str) -> None:
            state = parse_qs(urlsplit(url).query).get("state", [None])[0]
            if state is None:
                raise RuntimeError("The provider's authorization URL carries no state")
            pending.state = state
            self._pending[state] = pending
            pending.url_future.set_result(url)

        async def callback() -> tuple[str, str | None]:
            return await asyncio.wait_for(pending.code_future, self._ttl)

        factory = OAuthAuthFactory(
            config=OAuthClientConfig(
                redirect_uri=self.callback_url,
                client_name=self._client_name,
                scopes=tuple(connection.scopes),
                authorization_timeout_seconds=float(self._ttl),
            ),
            backend=self._backend,
            redirect_handler=redirect,
            callback_handler=callback,
        )
        providers: list[AdapterOAuthProvider] = []

        def capturing_factory(endpoint: str, resolved: ResolvedCredential | None) -> httpx.Auth:
            provider = factory(endpoint, resolved)
            providers.append(provider)
            return provider

        sessions = StreamableHttpSessionFactory(
            auth_factory=capturing_factory,
            endpoint_policy=self._policy,
            transport=self._transport,
            # The owner signs in at the provider while this connection waits for them. Consent
            # screens take far longer than the 30 seconds that bound an ordinary request, so
            # the wait is bounded by how long the sign-in link is valid instead.
            timeout_seconds=float(self._ttl),
        )
        async with sessions.connect(server.endpoint, credential):
            pass
        for provider in providers:
            await provider.persist_discovery()
        tokens = await self._backend.get(namespace_for(server.endpoint, credential), "tokens")
        scope = getattr(tokens, "scope", None)
        if isinstance(scope, str) and scope.strip():
            await anyio.to_thread.run_sync(
                lambda: self._repository.set_connection_scopes(
                    connection.connection_id, scope.split()
                )
            )

    async def is_authorized(self, connection: ConnectionRecord) -> bool:
        """Whether usable provider tokens are already stored for this connection."""
        server = await anyio.to_thread.run_sync(
            lambda: self._repository.get_server(connection.server_id)
        )
        credential = ResolvedCredential(
            credential_ref=f"connection:{connection.connection_id}",
            access_token="",
            scopes=frozenset(connection.scopes),
            local_identity_id=connection.identity_id,
            connection_id=connection.connection_id,
            server_id=connection.server_id,
        )
        storage = NamespacedTokenStorage(
            self._backend,
            namespace_for(server.endpoint, credential),
            frozenset(connection.scopes),
        )
        return await storage.get_tokens() is not None

    async def _seed_client(self, server_id: str, namespace: str) -> None:
        seed = self._clients.get(server_id)
        if seed is None or await self._backend.get(namespace, "client_info") is not None:
            return
        await self._backend.set(
            namespace,
            "client_info",
            OAuthClientInformationFull(
                client_id=seed.client_id,
                client_secret=seed.client_secret,
                redirect_uris=[AnyUrl(self.callback_url)],
                token_endpoint_auth_method="client_secret_post" if seed.client_secret else "none",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                client_name=self._client_name,
            ),
        )

    # --- callback ------------------------------------------------------------------------

    async def _callback(self, request: Request) -> Response:
        self._expire()
        params = request.query_params
        state = params.get("state")
        pending = self._pending.get(state) if state else None
        presented = request.cookies.get(COOKIE_NAME, "")
        if pending is None or not hmac.compare_digest(presented, pending.cookie_secret):
            # Wrong browser, replay, or forged state: never accept an authorization code here.
            return page(
                "Sign-in not accepted",
                "This sign-in was not started in this browser or has expired. "
                "Request a new link from Muse.",
                400,
            )
        self._pending.pop(state or "", None)
        assert pending.task is not None
        if pending.code_future.done() or pending.task.done():
            # The adapter's own connection ended before the provider answered (it timed out or
            # failed), so there is nothing left to hand the code to.
            return page(
                "Sign-in expired",
                "This sign-in took too long or was interrupted before it could be completed. "
                "Request a new link from Muse and try again.",
                400,
            )
        error = params.get("error")
        code = params.get("code")
        if error or not code:
            pending.code_future.set_exception(RuntimeError(error or "no authorization code"))
        else:
            pending.code_future.set_result((code, state))
        try:
            await asyncio.wait_for(asyncio.shield(pending.task), timeout=60)
        except Exception:
            return page(
                "Not connected",
                "The service did not complete the sign-in. Return to Muse and try again.",
                400,
            )
        if pending.continue_to:
            return redirect(f"{self._public_url}{pending.continue_to}")
        return page("Connected", "Your account is connected. You can return to Muse.")

    def _expire(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(seconds=self._ttl)
        for pending in [item for item in self._pending.values() if item.started_at < cutoff]:
            self._discard(pending)

    def _discard(self, pending: _Pending) -> None:
        if pending.state is not None:
            self._pending.pop(pending.state, None)
        if pending.task is not None and not pending.task.done():
            pending.task.cancel()


def runtime_auth_factory(
    repository: SqliteControlPlaneRepository,
    backend: OAuthStorageBackend,
    *,
    public_url: str,
    client_name: str = "Muse MCP Adapter",
) -> Callable[[str, ResolvedCredential | None], httpx.Auth | None]:
    """Per-request OAuth for linked accounts that never starts an interactive flow.

    Stored tokens are used and refreshed automatically. If they are missing or cannot be
    refreshed, the call fails fast with a reauthorization error instead of waiting for a browser.
    """
    callback_url = f"{public_url.rstrip('/')}{CALLBACK_PATH}"

    def factory(endpoint: str, credential: ResolvedCredential | None) -> httpx.Auth | None:
        if credential is None or not credential.connection_id:
            return None
        try:
            connection = repository.get_connection(credential.connection_id)
        except EntityNotFoundError:
            return None  # a static token for administrative discovery has no connection
        if connection.credential_kind != "oauth":
            return None
        message = f"The account linked to '{connection.label}' must be authorized again."

        async def refuse_redirect(url: str) -> None:
            del url
            raise McpReauthorizationRequired(message)

        async def refuse_callback() -> tuple[str, str | None]:
            raise McpReauthorizationRequired(message)

        return OAuthAuthFactory(
            config=OAuthClientConfig(
                redirect_uri=callback_url,
                client_name=client_name,
                scopes=tuple(connection.scopes),
            ),
            backend=backend,
            redirect_handler=refuse_redirect,
            callback_handler=refuse_callback,
        )(endpoint, credential)

    return factory


def _safe_path(value: str | None) -> bool:
    """Only same-site absolute paths may be used as a post-sign-in destination."""
    return value is not None and value.startswith("/") and not value.startswith("//")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
