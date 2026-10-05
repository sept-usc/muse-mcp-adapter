from contextlib import AbstractAsyncContextManager
from typing import cast

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import LATEST_PROTOCOL_VERSION

from muse_mcp_adapter.domain.models import ResolvedCredential, ServerRegistration
from muse_mcp_adapter.transport.sdk_client import (
    InMemorySessionFactory,
    McpSdkClient,
    StreamableHttpSessionFactory,
)
from muse_mcp_adapter.transport.security import EndpointSecurityPolicy


def _server() -> FastMCP:
    server = FastMCP(
        "Phase Five Conformance",
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["localhost", "localhost:*"],
        ),
    )

    @server.tool()
    def find_issue(issue_id: str) -> dict[str, str]:
        return {"id": issue_id, "title": "Transport verified"}

    @server.resource("mcp://workspace/guide", name="workspace_guide")
    def workspace_guide() -> str:
        return "# Workspace guide\nUse the published contract."

    @server.prompt(name="triage_issue")
    def triage_issue(issue_id: str) -> str:
        return f"Triage {issue_id} using the workspace guide."

    return server


def test_official_sdk_covers_discovery_tools_resources_and_prompts() -> None:
    client = McpSdkClient(InMemorySessionFactory(_server()))
    registration = ServerRegistration(
        server_id="conformance",
        display_name="Conformance",
        endpoint="https://mcp.example.test/mcp",
        auth_profile="bearer",
        provider_pack=None,
        enabled=True,
        created_at="2026-10-04T18:00:00Z",
        updated_at="2026-10-04T18:00:00Z",
    )
    credential = ResolvedCredential("vault://connection/a", "secret")

    snapshot = client.discover(registration)
    keys = {(item["type"], item["name"]) for item in snapshot.primitives}
    assert snapshot.protocol_version == LATEST_PROTOCOL_VERSION
    assert keys == {
        ("tool", "find_issue"),
        ("resource", "workspace_guide"),
        ("prompt", "triage_issue"),
    }

    resource = client.read_resource(
        endpoint=registration.endpoint,
        uri="mcp://workspace/guide",
        credential=credential,
    )
    assert resource.contents[0].text == "# Workspace guide\nUse the published contract."

    prompt = client.get_prompt(
        endpoint=registration.endpoint,
        name="triage_issue",
        arguments={"issue_id": "ISSUE-42"},
        credential=credential,
    )
    assert prompt.messages[0].content[0].text == ("Triage ISSUE-42 using the workspace guide.")

    tool = client.call_tool(
        endpoint=registration.endpoint,
        name="find_issue",
        arguments={"issue_id": "ISSUE-42"},
        credential=credential,
        idempotency_key=None,
        timeout_seconds=5,
    )
    assert tool.structured_content == {
        "id": "ISSUE-42",
        "title": "Transport verified",
    }


def test_streamable_http_transport_against_local_stateless_server() -> None:
    server = _server()
    app = server.streamable_http_app()
    endpoint = "https://localhost/mcp"
    client = McpSdkClient(
        StreamableHttpSessionFactory(
            transport=httpx.ASGITransport(app=app),
            lifespan_factory=lambda: cast(
                AbstractAsyncContextManager[object], app.router.lifespan_context(app)
            ),
            endpoint_policy=EndpointSecurityPolicy(allow_private_networks=True),
        )
    )
    snapshot = client.discover(
        ServerRegistration(
            server_id="http-conformance",
            display_name="HTTP Conformance",
            endpoint=endpoint,
            auth_profile="none",
            provider_pack=None,
            enabled=True,
            created_at="2026-10-04T18:00:00Z",
            updated_at="2026-10-04T18:00:00Z",
        )
    )
    assert {item["type"] for item in snapshot.primitives} == {
        "tool",
        "resource",
        "prompt",
    }
