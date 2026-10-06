"""Assemble Atlas: the MCP server, its authentication, fault injection and control routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from .auth import ALL_SCOPES, BearerMiddleware, DemoAuthorizationServer, ScopeContext
from .chaos import ChaosMiddleware
from .server import AtlasServer
from .state import AtlasState


@dataclass(frozen=True)
class AtlasOptions:
    host: str = "127.0.0.1"
    port: int = 9101
    auth: Literal["none", "bearer", "oauth"] = "none"
    bearer_token: str = "atlas-secret-token"
    stateless: bool = False  # sampling and elicitation need stateful sessions
    access_ttl_seconds: int = 3600


@dataclass
class AtlasApp:
    asgi: ASGIApp
    state: AtlasState
    server: AtlasServer
    provider: DemoAuthorizationServer | None


def build_app(options: AtlasOptions | None = None) -> AtlasApp:
    options = options or AtlasOptions()
    state = AtlasState()
    provider: DemoAuthorizationServer | None = None
    auth_kwargs: dict[str, Any] | None = None
    scopes: ScopeContext | None = None

    if options.auth == "oauth":
        base = f"http://{options.host}:{options.port}"
        provider = DemoAuthorizationServer(options.access_ttl_seconds)
        auth_kwargs = {
            "auth_server_provider": provider,
            "auth": AuthSettings(
                issuer_url=AnyHttpUrl(base),
                resource_server_url=AnyHttpUrl(f"{base}/mcp"),
                required_scopes=["issues:read"],
                validate_token_resource=False,
                client_registration_options=ClientRegistrationOptions(
                    enabled=True, valid_scopes=ALL_SCOPES, default_scopes=["issues:read"]
                ),
            ),
        }
        scopes = ScopeContext()

    server = AtlasServer(
        state,
        host=options.host,
        port=options.port,
        stateless=options.stateless,
        auth_kwargs=auth_kwargs,
        scope_checker=scopes,
    )
    _register_control_routes(server, state, provider)

    asgi: ASGIApp = ChaosMiddleware(server.mcp.streamable_http_app(), state)
    if options.auth == "bearer":
        asgi = BearerMiddleware(asgi, options.bearer_token)
    return AtlasApp(asgi, state, server, provider)


def _register_control_routes(
    server: AtlasServer, state: AtlasState, provider: DemoAuthorizationServer | None
) -> None:
    """Routes a test uses to inspect and steer the server. They are not part of MCP."""
    mcp = server.mcp

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/_control/state", methods=["GET"])
    async def get_state(_: Request) -> Response:
        return JSONResponse(state.snapshot())

    @mcp.custom_route("/_control/ledger", methods=["GET"])
    async def ledger(_: Request) -> Response:
        return JSONResponse(state.ledger())

    @mcp.custom_route("/_control/reset", methods=["POST"])
    async def reset(_: Request) -> Response:
        state.reset()
        server.set_profile("v1")
        return JSONResponse({"status": "reset"})

    @mcp.custom_route("/_control/faults", methods=["POST"])
    async def faults(request: Request) -> Response:
        state.set_faults(await request.json())
        return JSONResponse({"faults": list(state.faults)})

    @mcp.custom_route("/_control/profile", methods=["POST"])
    async def profile(request: Request) -> Response:
        server.set_profile((await request.json())["profile"])
        return JSONResponse({"profile": state.profile})

    @mcp.custom_route("/_control/redirect", methods=["POST"])
    async def redirect(request: Request) -> Response:
        target = (await request.json()).get("to")
        state.redirect_to = target
        return JSONResponse({"redirectTo": target})

    @mcp.custom_route("/_control/oauth/expire", methods=["POST"])
    async def expire(_: Request) -> Response:
        if provider is None:
            return JSONResponse({"error": "not in oauth mode"}, status_code=409)
        provider.expire_access_tokens()
        return JSONResponse({"status": "expired"})

    @mcp.custom_route("/_control/oauth/revoke", methods=["POST"])
    async def revoke(_: Request) -> Response:
        if provider is None:
            return JSONResponse({"error": "not in oauth mode"}, status_code=409)
        provider.revoke_everything()
        return JSONResponse({"status": "revoked"})
