"""A local OAuth 2.1 authorization server plus a protected MCP server, built on the SDK.

Consent is granted automatically so tests need no browser: the authorization endpoint
redirects straight back with a code, exactly as a real provider does after the user approves.
"""

import secrets
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, AnyUrl

ALL_SCOPES = ["issues:read", "issues:write"]


class FakeAuthorizationServer:
    def __init__(self, access_ttl_seconds: int = 3600) -> None:
        self._ttl = access_ttl_seconds
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.access: dict[str, AccessToken] = {}
        self.refresh: dict[str, RefreshToken] = {}
        self.authorizations: list[AuthorizationParams] = []
        self.authorized_clients: list[str] = []  # which app each sign-in was for
        self.token_requests = 0
        self.grant_only: list[str] | None = None  # simulate a user who approves fewer scopes

    # --- test controls -----------------------------------------------------------------

    def expire_access_tokens(self) -> None:
        for token in self.access.values():
            token.expires_at = int(time.time()) - 10

    def revoke_everything(self) -> None:
        self.access.clear()
        self.refresh.clear()

    def register_app(self, client_id: str, client_secret: str | None, redirect_uri: str) -> None:
        """An app its owner registered by hand, for a provider with no dynamic registration."""
        self.clients[client_id] = OAuthClientInformationFull(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uris=[AnyUrl(redirect_uri)],
            token_endpoint_auth_method="client_secret_post" if client_secret else "none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=" ".join(ALL_SCOPES),
        )

    # --- provider interface ------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        assert client_info.client_id is not None
        self.clients[client_info.client_id] = client_info

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        self.authorizations.append(params)
        self.authorized_clients.append(str(client.client_id))
        scopes = self.grant_only or params.scopes or ["issues:read"]
        code = secrets.token_urlsafe(16)
        self.codes[code] = AuthorizationCode(
            code=code,
            scopes=scopes,
            expires_at=time.time() + 300,
            client_id=str(client.client_id),
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self.codes.get(authorization_code)
        return code if code is not None and code.client_id == client.client_id else None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self.token_requests += 1
        self.codes.pop(authorization_code.code, None)
        return self._issue(str(client.client_id), authorization_code.scopes)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        token = self.refresh.get(refresh_token)
        return token if token is not None and token.client_id == client.client_id else None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        self.token_requests += 1
        self.refresh.pop(refresh_token.token, None)
        return self._issue(str(client.client_id), scopes or refresh_token.scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        found = self.access.get(token)
        if found is None or (found.expires_at is not None and found.expires_at < time.time()):
            return None
        return found

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.access.pop(token.token, None)
        self.refresh.pop(token.token, None)

    def _issue(self, client_id: str, scopes: list[str]) -> OAuthToken:
        access = f"at-{secrets.token_hex(8)}"
        refresh = f"rt-{secrets.token_hex(8)}"
        expires = int(time.time()) + self._ttl
        self.access[access] = AccessToken(
            token=access, client_id=client_id, scopes=scopes, expires_at=expires
        )
        self.refresh[refresh] = RefreshToken(token=refresh, client_id=client_id, scopes=scopes)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self._ttl,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )


def protected_server(
    provider: FakeAuthorizationServer, port: int, *, registration: bool = True
) -> FastMCP:
    """An MCP server that only answers callers holding a token from ``provider``.

    With ``registration`` off, programs cannot register themselves: only apps added with
    ``provider.register_app`` can sign in, as with Slack, Asana or HubSpot.
    """
    base = f"http://127.0.0.1:{port}"
    server = FastMCP(
        "Protected",
        auth_server_provider=provider,  # type: ignore[arg-type]
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(base),
            resource_server_url=AnyHttpUrl(f"{base}/mcp"),
            required_scopes=["issues:read"],
            validate_token_resource=False,
            client_registration_options=ClientRegistrationOptions(
                enabled=registration, valid_scopes=ALL_SCOPES, default_scopes=["issues:read"]
            ),
        ),
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*"],
        ),
    )

    @server.tool()
    def find_issue(issue_id: str) -> dict[str, str]:
        return {"id": issue_id, "title": "Example"}

    return server
