import anyio
from mcp.shared.auth import OAuthToken

from muse_mcp_adapter.transport.oauth import (
    InMemoryOAuthStorageBackend,
    NamespacedTokenStorage,
)


def test_oauth_tokens_are_isolated_by_connection_namespace() -> None:
    async def scenario() -> None:
        backend = InMemoryOAuthStorageBackend()
        first = NamespacedTokenStorage(backend, "server|connection-a")
        second = NamespacedTokenStorage(backend, "server|connection-b")

        await first.set_tokens(OAuthToken(access_token="token-a", token_type="Bearer"))
        await second.set_tokens(OAuthToken(access_token="token-b", token_type="Bearer"))

        first_token = await first.get_tokens()
        second_token = await second.get_tokens()
        assert first_token is not None and first_token.access_token == "token-a"
        assert second_token is not None and second_token.access_token == "token-b"

    anyio.run(scenario)
