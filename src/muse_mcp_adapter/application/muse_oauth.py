"""The OAuth authorization server Muse signs in to when the owner adds a connector.

Each connector is its own issuer (``<public url>/c/<slug>``). When Muse starts account linking:

1. Muse discovers the endpoints, registers itself as a client, and opens the authorize page in
   the owner's browser (PKCE, S256 only).
2. The owner proves they are the owner by entering the connection code that ``link`` printed.
3. If the connection also needs the provider's own sign-in, the browser is taken through it and
   returns here.
4. Muse receives a single-use code and exchanges it for a short-lived access token and a rotating
   refresh token. Muse never sees the provider's tokens.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..connections import ConnectionService
from ..connections.service import ACCESS_TOKEN_PREFIX, hash_token
from ..domain.errors import EntityNotFoundError
from ..domain.models import ConnectionRecord
from ..persistence.oauth_store import (
    AuthCode,
    AuthRequest,
    OAuthClient,
    OAuthServerStore,
    StoredToken,
)
from ..persistence.sqlite_repository import SqliteControlPlaneRepository
from .oauth_web import OAuthWebFlow
from .web_pages import page, redirect

SCOPE = "connector"
REFRESH_TOKEN_PREFIX = "mcpa_rt_"
CSRF_COOKIE = "muse_adapter_csrf"
CONSENT_COOKIE = "muse_adapter_consent"
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})
_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")


@dataclass(frozen=True, slots=True)
class OAuthServerSettings:
    code_ttl_seconds: int = 60
    access_ttl_seconds: int = 3600
    refresh_ttl_seconds: int = 30 * 24 * 3600
    request_ttl_seconds: int = 600
    max_failed_attempts: int = 5
    lockout_seconds: int = 900
    max_clients_per_connection: int = 20


class _OAuthError(Exception):
    def __init__(
        self,
        error: str,
        description: str,
        status: int = 400,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.error = error
        self.description = description
        self.status = status
        self.headers = dict(headers or {})
        super().__init__(description)


class MuseAuthorizationServer:
    def __init__(
        self,
        *,
        repository: SqliteControlPlaneRepository,
        connections: ConnectionService,
        public_url: str,
        clock: Callable[[], str],
        web_flow: OAuthWebFlow | None = None,
        settings: OAuthServerSettings | None = None,
    ) -> None:
        self._connections = connections
        self._store = OAuthServerStore(repository)
        self._public_url = public_url.rstrip("/")
        self._secure = self._public_url.startswith("https://")
        self._clock = clock
        self._web_flow = web_flow
        self._settings = settings or OAuthServerSettings()

    def issuer(self, slug: str) -> str:
        return f"{self._public_url}/c/{slug}"

    def metadata_url(self, slug: str) -> str:
        return f"{self._public_url}/.well-known/oauth-protected-resource/c/{slug}"

    def routes(self) -> list[Route]:
        return [
            Route("/.well-known/oauth-protected-resource/c/{slug}", self._resource_metadata),
            Route("/c/{slug}/.well-known/oauth-protected-resource", self._resource_metadata),
            Route("/.well-known/oauth-authorization-server/c/{slug}", self._server_metadata),
            Route("/c/{slug}/.well-known/oauth-authorization-server", self._server_metadata),
            Route("/.well-known/openid-configuration/c/{slug}", self._server_metadata),
            Route("/c/{slug}/.well-known/openid-configuration", self._server_metadata),
            Route("/c/{slug}/oauth/register", self._register, methods=["POST"]),
            Route("/c/{slug}/oauth/authorize", self._authorize, methods=["GET"]),
            Route("/c/{slug}/oauth/consent", self._consent, methods=["POST"]),
            Route("/c/{slug}/oauth/continue", self._continue, methods=["GET"]),
            Route("/c/{slug}/oauth/token", self._token, methods=["POST"]),
            Route("/c/{slug}/oauth/revoke", self._revoke, methods=["POST"]),
        ]

    # --- discovery ---------------------------------------------------------------------

    async def _resource_metadata(self, request: Request) -> Response:
        slug = request.path_params["slug"]
        connection = await self._connection(slug)
        if connection is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        issuer = self.issuer(slug)
        return JSONResponse(
            {
                "resource": issuer,
                "authorization_servers": [issuer],
                "bearer_methods_supported": ["header"],
                "scopes_supported": [SCOPE],
                "resource_name": connection.label,
            }
        )

    async def _server_metadata(self, request: Request) -> Response:
        slug = request.path_params["slug"]
        if await self._connection(slug) is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        issuer = self.issuer(slug)
        return JSONResponse(
            {
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}/oauth/authorize",
                "token_endpoint": f"{issuer}/oauth/token",
                "registration_endpoint": f"{issuer}/oauth/register",
                "revocation_endpoint": f"{issuer}/oauth/revoke",
                "response_types_supported": ["code"],
                "response_modes_supported": ["query"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": list(_AUTH_METHODS),
                "scopes_supported": [SCOPE],
                "authorization_response_iss_parameter_supported": True,
            }
        )

    # --- dynamic client registration ---------------------------------------------------

    async def _register(self, request: Request) -> Response:
        slug = request.path_params["slug"]
        connection = await self._connection(slug)
        if connection is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            return _json_error("invalid_client_metadata", "Body must be JSON")
        if not isinstance(body, dict):
            return _json_error("invalid_client_metadata", "Body must be a JSON object")
        try:
            response = await anyio.to_thread.run_sync(
                lambda: self._register_client(connection, cast(dict[str, object], body))
            )
        except _OAuthError as error:
            return _json_error(error.error, error.description, error.status)
        return JSONResponse(response, status_code=201, headers=_NO_STORE)

    def _register_client(
        self, connection: ConnectionRecord, body: Mapping[str, object]
    ) -> dict[str, object]:
        uris = body.get("redirect_uris")
        if (
            not isinstance(uris, list)
            or not 1 <= len(cast(list[object], uris)) <= 5
            or not all(
                isinstance(item, str) and _valid_redirect_uri(item)
                for item in cast(list[object], uris)
            )
        ):
            raise _OAuthError(
                "invalid_redirect_uri",
                "redirect_uris must list 1 to 5 HTTPS URLs (or loopback HTTP URLs) "
                "without fragments",
            )
        method = body.get("token_endpoint_auth_method", "none")
        if method not in _AUTH_METHODS:
            raise _OAuthError("invalid_client_metadata", "Unsupported token_endpoint_auth_method")
        grants = body.get("grant_types", ["authorization_code", "refresh_token"])
        if (
            not isinstance(grants, list)
            or not set(cast(list[object], grants)) <= {"authorization_code", "refresh_token"}
            or "authorization_code" not in cast(list[object], grants)
        ):
            raise _OAuthError("invalid_client_metadata", "Unsupported grant_types")
        responses = body.get("response_types", ["code"])
        if not isinstance(responses, list) or set(cast(list[object], responses)) != {"code"}:
            raise _OAuthError("invalid_client_metadata", "Unsupported response_types")
        name = body.get("client_name", "Muse")
        if not isinstance(name, str):
            raise _OAuthError("invalid_client_metadata", "client_name must be a string")
        name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()[:100] or "Muse"

        if self._store.count_clients(connection.connection_id) >= (
            self._settings.max_clients_per_connection
        ):
            raise _OAuthError(
                "temporarily_unavailable", "Too many registered clients for this connector", 429
            )
        client_id = "mcpc_" + secrets.token_urlsafe(16)
        secret = secrets.token_urlsafe(32) if method != "none" else None
        client = OAuthClient(
            client_id=client_id,
            connection_id=connection.connection_id,
            client_name=name,
            redirect_uris=tuple(cast(list[str], uris)),
            auth_method=cast(str, method),
            secret_hash=hash_token(secret) if secret else None,
        )
        self._store.create_client(client, self._clock())
        response: dict[str, object] = {
            "client_id": client_id,
            "client_id_issued_at": int(_now(self._clock()).timestamp()),
            "client_name": name,
            "redirect_uris": list(client.redirect_uris),
            "grant_types": grants,
            "response_types": ["code"],
            "token_endpoint_auth_method": method,
            "scope": SCOPE,
        }
        if secret is not None:
            response["client_secret"] = secret
            response["client_secret_expires_at"] = 0
        return response

    # --- authorize and consent ---------------------------------------------------------

    async def _authorize(self, request: Request) -> Response:
        slug = request.path_params["slug"]
        connection = await self._connection(slug)
        if connection is None:
            return page("Not found", "This connector does not exist.", 404)
        params = request.query_params
        try:
            outcome = await anyio.to_thread.run_sync(
                lambda: self._begin_authorization(connection, dict(params))
            )
        except _OAuthError as error:
            # The client or redirect URI cannot be trusted, so never redirect: show the error.
            return page("Cannot connect", error.description, error.status)
        if isinstance(outcome, str):
            return redirect(outcome)  # a protocol error reported to the registered client
        request_id, client, error_message = outcome
        csrf = secrets.token_urlsafe(24)
        response = page(
            f"Connect {client.client_name}",
            f"{client.client_name} is asking to use “{connection.label}”.",
            extra=self._consent_form(slug, request_id, csrf, error_message),
        )
        response.set_cookie(
            CSRF_COOKIE,
            csrf,
            max_age=self._settings.request_ttl_seconds,
            httponly=True,
            secure=self._secure,
            samesite="lax",
            path=f"/c/{slug}/oauth",
        )
        return response

    def _begin_authorization(
        self, connection: ConnectionRecord, params: Mapping[str, str]
    ) -> str | tuple[str, OAuthClient, str | None]:
        client = self._store.get_client(params.get("client_id", ""))
        if client is None or client.connection_id != connection.connection_id:
            raise _OAuthError("invalid_client", "Unknown client")
        redirect_uri = params.get("redirect_uri", "")
        if redirect_uri not in client.redirect_uris:
            raise _OAuthError("invalid_request", "The redirect address is not registered")

        state = params.get("state")
        if state is not None and len(state) > 512:
            return self._error_redirect(
                redirect_uri, connection, "invalid_request", "state is too long", None
            )
        if params.get("response_type") != "code":
            return self._error_redirect(
                redirect_uri, connection, "unsupported_response_type", "Only code", state
            )
        challenge = params.get("code_challenge", "")
        if params.get("code_challenge_method") != "S256" or not _CHALLENGE.fullmatch(challenge):
            return self._error_redirect(
                redirect_uri, connection, "invalid_request", "PKCE with S256 is required", state
            )
        scopes = params.get("scope", SCOPE).split()
        if scopes and not set(scopes) <= {SCOPE}:
            return self._error_redirect(
                redirect_uri, connection, "invalid_scope", "Unsupported scope", state
            )
        resource = params.get("resource")
        if resource is not None and resource.rstrip("/") != self.issuer(connection.slug):
            return self._error_redirect(
                redirect_uri, connection, "invalid_target", "Unknown resource", state
            )

        request_id = secrets.token_urlsafe(24)
        now = _now(self._clock())
        self._store.create_auth_request(
            AuthRequest(
                request_hash=hash_token(request_id),
                connection_id=connection.connection_id,
                client_id=client.client_id,
                redirect_uri=redirect_uri,
                state=state,
                code_challenge=challenge,
                scope=SCOPE,
                cookie_hash=None,
                expires_at=_iso(now + timedelta(seconds=self._settings.request_ttl_seconds)),
                completed_at=None,
            ),
            _iso(now),
        )
        return request_id, client, None

    def _consent_form(self, slug: str, request_id: str, csrf: str, error: str | None) -> str:
        message = f"<p style='color:#b00020'>{html.escape(error)}</p>" if error else ""
        return (
            f"{message}<p>Enter the connection code shown when this connector was created.</p>"
            f"<form method=post action='{html.escape(self.issuer(slug))}/oauth/consent'>"
            f"<input type=hidden name=request value='{html.escape(request_id)}'>"
            f"<input type=hidden name=csrf value='{html.escape(csrf)}'>"
            "<input name=code autocomplete=off autocapitalize=characters required autofocus "
            "style='font-size:1.2rem;padding:.4rem;width:100%;box-sizing:border-box'>"
            "<p><button type=submit style='font-size:1rem;padding:.5rem 1.2rem'>"
            "Connect</button></p>"
            "</form>"
        )

    async def _consent(self, request: Request) -> Response:
        slug = request.path_params["slug"]
        connection = await self._connection(slug)
        if connection is None:
            return page("Not found", "This connector does not exist.", 404)
        form = await request.form()
        request_id = str(form.get("request", ""))
        csrf = str(form.get("csrf", ""))
        code = str(form.get("code", ""))
        cookie = request.cookies.get(CSRF_COOKIE, "")
        if not csrf or not hmac.compare_digest(cookie, csrf):
            return page("Request not accepted", "Start again from Muse.", 400)

        verdict = await anyio.to_thread.run_sync(
            lambda: self._check_consent(connection, request_id, code)
        )
        if isinstance(verdict, tuple):
            status, message = verdict
            if status == 401:  # wrong code: let the owner try again on the same request
                return page(
                    "Connect",
                    "The code was not accepted.",
                    401,
                    extra=self._consent_form(slug, request_id, csrf, message),
                )
            return page("Cannot connect", message, status)

        consent_secret = secrets.token_urlsafe(24)
        await anyio.to_thread.run_sync(
            lambda: self._store.bind_auth_request_cookie(
                hash_token(request_id), hash_token(consent_secret)
            )
        )
        needs_provider = (
            self._web_flow is not None
            and connection.credential_kind == "oauth"
            and not await self._web_flow.is_authorized(connection)
        )
        if needs_provider and self._web_flow is not None:
            flow = self._web_flow
            target = await anyio.to_thread.run_sync(
                lambda: flow.login_link(
                    connection.connection_id,
                    f"/c/{slug}/oauth/continue?req={quote(request_id, safe='')}",
                )
            )
        else:
            target = await anyio.to_thread.run_sync(lambda: self._finish(connection, request_id))
        response = redirect(target)
        response.set_cookie(
            CONSENT_COOKIE,
            consent_secret,
            max_age=self._settings.request_ttl_seconds,
            httponly=True,
            secure=self._secure,
            samesite="lax",
            path=f"/c/{slug}/oauth",
        )
        return response

    def _check_consent(
        self, connection: ConnectionRecord, request_id: str, code: str
    ) -> tuple[int, str] | None:
        pending = self._store.get_auth_request(hash_token(request_id))
        now = _now(self._clock())
        if (
            pending is None
            or pending.connection_id != connection.connection_id
            or pending.completed_at is not None
            or pending.expires_at <= _iso(now)
        ):
            return 400, "This sign-in request expired. Start again from Muse."
        since = _iso(now - timedelta(seconds=self._settings.lockout_seconds))
        if (
            self._store.count_attempts_since(connection.connection_id, since)
            >= self._settings.max_failed_attempts
        ):
            return 429, "Too many incorrect codes. Try again later."
        if not self._connections.verify_link_code(connection.connection_id, code):
            self._store.record_attempt(connection.connection_id, _iso(now))
            return 401, "Check the code and try again, or create a new one with the CLI."
        self._store.clear_attempts(connection.connection_id)
        return None

    async def _continue(self, request: Request) -> Response:
        slug = request.path_params["slug"]
        connection = await self._connection(slug)
        if connection is None:
            return page("Not found", "This connector does not exist.", 404)
        request_id = request.query_params.get("req", "")
        consent = request.cookies.get(CONSENT_COOKIE, "")
        authorized = (
            self._web_flow is None
            or connection.credential_kind != "oauth"
            or await self._web_flow.is_authorized(
                await anyio.to_thread.run_sync(
                    lambda: self._connections.get(connection.connection_id)
                )
            )
        )
        if not authorized:
            return page("Not connected", "The sign-in did not finish. Start again from Muse.", 400)
        try:
            target = await anyio.to_thread.run_sync(
                lambda: self._finish_bound(connection, request_id, consent)
            )
        except _OAuthError as error:
            return page("Cannot connect", error.description, error.status)
        return redirect(target)

    def _finish_bound(self, connection: ConnectionRecord, request_id: str, consent: str) -> str:
        pending = self._store.get_auth_request(hash_token(request_id))
        if (
            pending is None
            or pending.cookie_hash is None
            or not hmac.compare_digest(pending.cookie_hash, hash_token(consent))
        ):
            raise _OAuthError("access_denied", "This sign-in was not started in this browser")
        return self._finish(connection, request_id)

    def _finish(self, connection: ConnectionRecord, request_id: str) -> str:
        """Spend the authorization request and hand Muse its single-use code."""
        pending = self._store.get_auth_request(hash_token(request_id))
        now = _now(self._clock())
        if (
            pending is None
            or pending.connection_id != connection.connection_id
            or not self._store.complete_auth_request(hash_token(request_id), _iso(now))
        ):
            raise _OAuthError("access_denied", "This sign-in request expired or was already used")
        code = secrets.token_urlsafe(32)
        self._store.create_code(
            AuthCode(
                code_hash=hash_token(code),
                connection_id=connection.connection_id,
                client_id=pending.client_id,
                redirect_uri=pending.redirect_uri,
                code_challenge=pending.code_challenge,
                scope=pending.scope,
                family_id=secrets.token_hex(16),
                expires_at=_iso(now + timedelta(seconds=self._settings.code_ttl_seconds)),
                used_at=None,
            ),
            _iso(now),
        )
        return _with_query(
            pending.redirect_uri,
            {"code": code, "state": pending.state, "iss": self.issuer(connection.slug)},
        )

    def _error_redirect(
        self,
        redirect_uri: str,
        connection: ConnectionRecord,
        error: str,
        description: str,
        state: str | None,
    ) -> str:
        return _with_query(
            redirect_uri,
            {
                "error": error,
                "error_description": description,
                "state": state,
                "iss": self.issuer(connection.slug),
            },
        )

    # --- token endpoint ----------------------------------------------------------------

    async def _token(self, request: Request) -> Response:
        slug = request.path_params["slug"]
        connection = await self._connection(slug)
        if connection is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        form = await request.form()
        fields = {key: str(value) for key, value in form.items()}
        authorization = request.headers.get("authorization")
        try:
            body = await anyio.to_thread.run_sync(
                lambda: self._issue(connection, fields, authorization)
            )
        except _OAuthError as error:
            return _json_error(error.error, error.description, error.status, error.headers)
        return JSONResponse(body, headers=_NO_STORE)

    def _issue(
        self,
        connection: ConnectionRecord,
        fields: Mapping[str, str],
        authorization: str | None,
    ) -> dict[str, object]:
        client = self._authenticate_client(connection, fields, authorization)
        grant = fields.get("grant_type")
        now = _now(self._clock())
        if grant == "authorization_code":
            family = self._redeem_code(connection, client, fields, now)
        elif grant == "refresh_token":
            family = self._redeem_refresh(connection, client, fields, now)
        else:
            raise _OAuthError("unsupported_grant_type", "Unsupported grant_type")
        return self._new_tokens(connection, client, family, now)

    def _authenticate_client(
        self,
        connection: ConnectionRecord,
        fields: Mapping[str, str],
        authorization: str | None,
    ) -> OAuthClient:
        client_id = fields.get("client_id", "")
        secret = fields.get("client_secret")
        if authorization and authorization.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(authorization[6:].strip(), validate=True).decode()
                raw_id, _, raw_secret = decoded.partition(":")
                client_id, secret = unquote(raw_id), unquote(raw_secret)
            except ValueError:
                raise _OAuthError(
                    "invalid_client", "Malformed credentials", 401, _BASIC_CHALLENGE
                ) from None
        client = self._store.get_client(client_id)
        if client is None or client.connection_id != connection.connection_id:
            raise _OAuthError("invalid_client", "Unknown client", 401, _BASIC_CHALLENGE)
        if client.auth_method != "none" and (
            secret is None
            or client.secret_hash is None
            or not hmac.compare_digest(hash_token(secret), client.secret_hash)
        ):
            raise _OAuthError(
                "invalid_client", "Client authentication failed", 401, _BASIC_CHALLENGE
            )
        return client

    def _redeem_code(
        self,
        connection: ConnectionRecord,
        client: OAuthClient,
        fields: Mapping[str, str],
        now: datetime,
    ) -> str:
        code, spent = self._store.take_code(hash_token(fields.get("code", "")), _iso(now))
        if code is None:
            raise _OAuthError("invalid_grant", "The code is not valid")
        if not spent:
            # A code used twice means it leaked: end everything issued from it.
            self._store.revoke_family(code.family_id, _iso(now))
            raise _OAuthError("invalid_grant", "The code was already used")
        verifier = fields.get("code_verifier", "")
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        if (
            code.connection_id != connection.connection_id
            or code.client_id != client.client_id
            or code.redirect_uri != fields.get("redirect_uri")
            or code.expires_at <= _iso(now)
            or not _VERIFIER.fullmatch(verifier)
            or not hmac.compare_digest(expected.rstrip(b"=").decode(), code.code_challenge)
        ):
            raise _OAuthError("invalid_grant", "The code cannot be exchanged for these details")
        return code.family_id

    def _redeem_refresh(
        self,
        connection: ConnectionRecord,
        client: OAuthClient,
        fields: Mapping[str, str],
        now: datetime,
    ) -> str:
        stored = self._store.get_token(hash_token(fields.get("refresh_token", "")))
        if (
            stored is None
            or stored.kind != "refresh"
            or stored.connection_id != connection.connection_id
            or stored.client_id != client.client_id
            or stored.revoked_at is not None
            or stored.expires_at <= _iso(now)
        ):
            raise _OAuthError("invalid_grant", "The refresh token is not valid")
        scope = fields.get("scope")
        if scope is not None and not set(scope.split()) <= set(stored.scope.split()):
            raise _OAuthError("invalid_scope", "Cannot widen the granted scope")
        if not self._store.use_refresh_token(stored.token_hash, _iso(now)):
            self._store.revoke_family(stored.family_id, _iso(now))  # reuse: assume theft
            raise _OAuthError("invalid_grant", "The refresh token was already used")
        return stored.family_id

    def _new_tokens(
        self, connection: ConnectionRecord, client: OAuthClient, family: str, now: datetime
    ) -> dict[str, object]:
        access = ACCESS_TOKEN_PREFIX + secrets.token_urlsafe(32)
        refresh = REFRESH_TOKEN_PREFIX + secrets.token_urlsafe(32)
        for kind, token, ttl in (
            ("access", access, self._settings.access_ttl_seconds),
            ("refresh", refresh, self._settings.refresh_ttl_seconds),
        ):
            self._store.create_token(
                StoredToken(
                    token_hash=hash_token(token),
                    kind=kind,
                    connection_id=connection.connection_id,
                    client_id=client.client_id,
                    scope=SCOPE,
                    family_id=family,
                    expires_at=_iso(now + timedelta(seconds=ttl)),
                    used_at=None,
                    revoked_at=None,
                ),
                _iso(now),
            )
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": self._settings.access_ttl_seconds,
            "refresh_token": refresh,
            "scope": SCOPE,
        }

    async def _revoke(self, request: Request) -> Response:
        slug = request.path_params["slug"]
        connection = await self._connection(slug)
        if connection is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        form = await request.form()
        fields = {key: str(value) for key, value in form.items()}
        authorization = request.headers.get("authorization")

        def revoke() -> None:
            client = self._authenticate_client(connection, fields, authorization)
            stored = self._store.get_token(hash_token(fields.get("token", "")))
            if stored is not None and stored.client_id == client.client_id:
                self._store.revoke_family(stored.family_id, self._clock())

        try:
            await anyio.to_thread.run_sync(revoke)
        except _OAuthError as error:
            return _json_error(error.error, error.description, error.status, error.headers)
        return Response(status_code=200, headers=_NO_STORE)

    async def _connection(self, slug: str) -> ConnectionRecord | None:
        try:
            return await anyio.to_thread.run_sync(lambda: self._connections.by_slug(slug))
        except EntityNotFoundError:
            return None


_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_BASIC_CHALLENGE = {"WWW-Authenticate": 'Basic realm="oauth"'}


def _json_error(
    error: str,
    description: str,
    status: int = 400,
    headers: Mapping[str, str] | None = None,
) -> Response:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status,
        headers={**_NO_STORE, **(headers or {})},
    )


def _valid_redirect_uri(uri: str) -> bool:
    if len(uri) > 2048 or any(ord(char) < 33 for char in uri):
        return False
    parsed = urlsplit(uri)
    if parsed.fragment or parsed.username is not None or parsed.password is not None:
        return False
    host = parsed.hostname
    if not host:
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and host in _LOOPBACK


def _with_query(uri: str, values: Mapping[str, str | None]) -> str:
    parsed = urlsplit(uri)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend((key, value) for key, value in values.items() if value is not None)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def _now(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
