"""The production transport (endpoint pinning included) against a real local socket."""

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from muse_mcp_adapter.domain.errors import UnsafeEndpointError
from muse_mcp_adapter.domain.models import ResolvedCredential, ServerRegistration
from muse_mcp_adapter.transport.sdk_client import McpSdkClient, StreamableHttpSessionFactory
from muse_mcp_adapter.transport.security import EndpointSecurityPolicy
from tests.fixtures.live_server import run_asgi

_LOCAL = EndpointSecurityPolicy(require_tls=False, allow_private_networks=True)


def _simple_server() -> FastMCP:
    server = FastMCP(
        "Simple",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*"],
        ),
    )

    @server.tool()
    def find_issue(issue_id: str) -> dict[str, str]:
        return {"id": issue_id, "title": "Example"}

    return server


def _registration(endpoint: str) -> ServerRegistration:
    return ServerRegistration(
        "simple",
        "Simple",
        endpoint,
        "none",
        None,
        True,
        "2026-10-05T00:00:00Z",
        "2026-10-05T00:00:00Z",
    )


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_discovery_and_tool_call_over_a_real_socket(host: str) -> None:
    with run_asgi(_simple_server().streamable_http_app()) as port:
        endpoint = f"http://{host}:{port}/mcp"
        client = McpSdkClient(StreamableHttpSessionFactory(endpoint_policy=_LOCAL))

        snapshot = client.discover(_registration(endpoint))
        assert [item["name"] for item in snapshot.primitives] == ["find_issue"]
        assert snapshot.server_info["name"] == "Simple"

        result = client.call_tool(
            endpoint=endpoint,
            name="find_issue",
            arguments={"issue_id": "ISSUE-1"},
            credential=ResolvedCredential("secret://x", "token"),
            idempotency_key=None,
        )
        assert result.structured_content == {"id": "ISSUE-1", "title": "Example"}


def test_default_policy_still_refuses_the_same_local_server() -> None:
    with run_asgi(_simple_server().streamable_http_app()) as port:
        client = McpSdkClient(StreamableHttpSessionFactory())
        with pytest.raises(UnsafeEndpointError):
            client.discover(_registration(f"http://127.0.0.1:{port}/mcp"))
