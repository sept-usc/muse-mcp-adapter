"""Transport classification, discovery fidelity and read-path hardening (review findings)."""

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import cast

import httpx
import pytest
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel

from muse_mcp_adapter.application.control_plane_service import ControlPlaneService
from muse_mcp_adapter.credentials.resolver import InMemoryCredentialResolver
from muse_mcp_adapter.data_plane.read_service import ReadDataPlaneService
from muse_mcp_adapter.data_plane.tool_client import McpInputRequired, McpPreDispatchError
from muse_mcp_adapter.discovery.client import ManifestDiscoveryClient
from muse_mcp_adapter.domain.errors import RequestValidationError
from muse_mcp_adapter.domain.models import (
    DataPlaneRequest,
    McpPromptResult,
    McpResourceContent,
    McpResourceResult,
    ResolvedCredential,
    ServerRegistration,
)
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from muse_mcp_adapter.transport.sdk_client import (
    InMemorySessionFactory,
    McpSdkClient,
    StreamableHttpSessionFactory,
)
from muse_mcp_adapter.transport.security import EndpointSecurityPolicy

_NOW = "2026-10-04T18:05:00Z"
_CREDENTIAL = ResolvedCredential("secret://x", "token")


def _server(name: str = "Review Conformance") -> FastMCP:
    server = FastMCP(
        name,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["localhost", "localhost:*"],
        ),
    )

    @server.tool()
    def find_issue(issue_id: str) -> dict[str, str]:
        return {"id": issue_id}

    @server.resource("mcp://workspace/big", name="big")
    def big() -> str:
        return "x" * 20_000

    return server


def _http_client(
    server: FastMCP, *, max_response_bytes: int = 16 * 1024 * 1024, timeout_seconds: float = 30.0
) -> McpSdkClient:
    app = server.streamable_http_app()
    return McpSdkClient(
        StreamableHttpSessionFactory(
            transport=httpx.ASGITransport(app=app),
            lifespan_factory=lambda: cast(
                AbstractAsyncContextManager[object], app.router.lifespan_context(app)
            ),
            endpoint_policy=EndpointSecurityPolicy(allow_private_networks=True),
            max_response_bytes=max_response_bytes,
            timeout_seconds=timeout_seconds,
        )
    )


def _registration() -> ServerRegistration:
    return ServerRegistration(
        server_id="review-server",
        display_name="Registered Display Name",
        endpoint="https://localhost/mcp",
        auth_profile="none",
        provider_pack=None,
        enabled=True,
        created_at=_NOW,
        updated_at=_NOW,
    )


# --- Finding 10: discovery records what the server reported ---------------------------------


def test_discovery_records_the_negotiated_server_identity_and_capabilities() -> None:
    snapshot = _http_client(_server("Review Conformance")).discover(_registration())

    assert snapshot.server_info["name"] == "Review Conformance"
    assert snapshot.server_info["name"] != "Registered Display Name"
    negotiated = cast(Mapping[str, object], snapshot.capabilities["negotiated"])
    assert "tools" in negotiated


# --- Finding 5: failure classification by the real client -----------------------------------


def test_unsafe_endpoint_is_a_pre_dispatch_failure_not_an_unknown_outcome() -> None:
    client = McpSdkClient(StreamableHttpSessionFactory())  # default policy forbids private nets
    with pytest.raises(McpPreDispatchError):
        client.call_tool(
            endpoint="https://127.0.0.1/mcp",
            name="find_issue",
            arguments={"issue_id": "1"},
            credential=_CREDENTIAL,
            idempotency_key="key-1",
        )


def test_unreachable_server_is_a_pre_dispatch_failure() -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = McpSdkClient(
        StreamableHttpSessionFactory(
            transport=httpx.MockTransport(refuse),
            endpoint_policy=EndpointSecurityPolicy(allow_private_networks=True),
        )
    )
    with pytest.raises(McpPreDispatchError):
        client.call_tool(
            endpoint="https://localhost/mcp",
            name="find_issue",
            arguments={},
            credential=_CREDENTIAL,
            idempotency_key=None,
        )


class _Team(BaseModel):
    team: str


def _elicitor() -> FastMCP:
    server = FastMCP("Elicitor")

    @server.tool()
    async def create_issue(title: str, ctx: Context) -> str:  # type: ignore[type-arg]
        answer = await ctx.elicit("Which team?", _Team)
        if answer.action != "accept":
            return "cancelled"
        return f"{title} -> {answer.data.team}"

    return server


def test_downstream_elicitation_surfaces_input_required_and_resumes_with_the_answer() -> None:
    client = McpSdkClient(InMemorySessionFactory(_elicitor()._mcp_server))  # pyright: ignore

    with pytest.raises(McpInputRequired) as raised:
        client.call_tool(
            endpoint="memory://",
            name="create_issue",
            arguments={"title": "Bug"},
            credential=_CREDENTIAL,
            idempotency_key="key-1",
        )
    assert "team" in str(raised.value.schema)

    result = client.call_tool(
        endpoint="memory://",
        name="create_issue",
        arguments={"title": "Bug"},
        credential=_CREDENTIAL,
        idempotency_key="key-1",
        elicitation_answer={"team": "platform"},
    )
    assert result.content[0].text == "Bug -> platform"


# --- Finding 8: response-size cap before the body is buffered -------------------------------


def test_oversized_downstream_response_is_aborted_by_the_transport() -> None:
    server = _server()
    small = _http_client(server, max_response_bytes=2_000, timeout_seconds=2.0)
    with pytest.raises(Exception):  # noqa: B017 - SDK wraps the abort in task-group errors
        small.read_resource(
            endpoint="https://localhost/mcp", uri="mcp://workspace/big", credential=_CREDENTIAL
        )

    roomy = _http_client(_server())
    result = roomy.read_resource(
        endpoint="https://localhost/mcp", uri="mcp://workspace/big", credential=_CREDENTIAL
    )
    assert len(result.contents[0].text or "") == 20_000


# --- Finding 8: URI scheme, argument constraints and URI normalization ----------------------


class _StubReadClient:
    def __init__(self) -> None:
        self.uris: list[str] = []
        self.reply_uri: str | None = None

    def read_resource(
        self, *, endpoint: str, uri: str, credential: ResolvedCredential
    ) -> McpResourceResult:
        del endpoint, credential
        self.uris.append(uri)
        return McpResourceResult(
            contents=(
                McpResourceContent(uri=self.reply_uri or uri, mime_type="text/plain", text="ok"),
            )
        )

    def get_prompt(
        self,
        *,
        endpoint: str,
        name: str,
        arguments: Mapping[str, str],
        credential: ResolvedCredential,
    ) -> McpPromptResult:
        raise AssertionError("not used")


def _read_service(tmp_path: Path, client: _StubReadClient) -> ReadDataPlaneService:
    manifest = {
        "protocolVersion": "2026-07-28",
        "serverInfo": {"name": "s"},
        "capabilities": {},
        "primitives": [
            {"type": "resource_template", "name": "issue", "uriTemplate": "mcp://issues/{issueId}"},
            {"type": "resource_template", "name": "broken", "uriTemplate": "mcp://a}b/{x}"},
            {"type": "resource", "name": "site", "uri": "https://docs.example.test"},
        ],
    }
    resource = {"allowedMimeTypes": ["text/plain"], "maxBytes": 1000}
    repository = SqliteControlPlaneRepository(tmp_path / "reads.db")
    repository.initialize()
    control = ControlPlaneService(repository, clock=lambda: _NOW)
    control.register_server(
        server_id="project-system",
        display_name="P",
        endpoint="https://mcp.example.test/mcp",
        auth_profile="bearer_token",
    )
    control.discover("project-system", ManifestDiscoveryClient(manifest))
    control.publish_catalog(
        server_id="project-system",
        catalog_version="1.0.0",
        policies=[
            {
                "primitiveKey": "resource_template:issue",
                "allowed": True,
                "publicOperationId": "read_issue",
                "classification": "read",
                "resource": {
                    **resource,
                    "allowedUriSchemes": ["mcp"],
                    "argumentPatterns": {"issueId": "[A-Z]+-[0-9]+"},
                },
            },
            {
                "primitiveKey": "resource_template:broken",
                "allowed": True,
                "publicOperationId": "read_broken",
                "classification": "read",
                "resource": resource,
            },
            {
                "primitiveKey": "resource:site",
                "allowed": True,
                "publicOperationId": "read_site",
                "classification": "read",
                "resource": {**resource, "allowedUriHosts": ["docs.example.test"]},
            },
        ],
    )
    control.activate_catalog("project-system", "1.0.0")
    resolver = InMemoryCredentialResolver()
    resolver.register(
        local_identity_id="user-1",
        connection_id="connection-1",
        server_id="project-system",
        credential_ref="secret://p/u",
        access_token="t",
        scopes=frozenset(),
    )
    return ReadDataPlaneService(repository, resolver, client)


def _read(operation: str, arguments: Mapping[str, object]) -> DataPlaneRequest:
    return DataPlaneRequest(
        request_id="r1",
        server_id="project-system",
        public_operation_id=operation,
        local_identity_id="user-1",
        connection_id="connection-1",
        arguments=arguments,
    )


def test_template_arguments_must_match_the_published_constraint(tmp_path: Path) -> None:
    client = _StubReadClient()
    service = _read_service(tmp_path, client)

    assert service.execute(_read("read_issue", {"issueId": "ABC-12"})).contents
    for value in ("abc-12", "ABC-12/../../etc/passwd", "ABC-12 OR 1=1", ".."):
        with pytest.raises(RequestValidationError):
            service.execute(_read("read_issue", {"issueId": value}))
    assert client.uris == ["mcp://issues/ABC-12"]


def test_malformed_template_is_a_validation_error_not_a_crash(tmp_path: Path) -> None:
    client = _StubReadClient()
    with pytest.raises(RequestValidationError, match="template syntax"):
        _read_service(tmp_path, client).execute(_read("read_broken", {"x": "1"}))
    assert client.uris == []


def test_host_only_uri_normalization_does_not_reject_valid_reads(tmp_path: Path) -> None:
    client = _StubReadClient()
    client.reply_uri = "https://docs.example.test/"  # the SDK adds a trailing slash
    response = _read_service(tmp_path, client).execute(_read("read_site", {}))
    assert response.contents[0].text == "ok"


def test_server_that_answers_with_a_different_uri_is_still_rejected(tmp_path: Path) -> None:
    from muse_mcp_adapter.domain.errors import DownstreamContentError

    client = _StubReadClient()
    client.reply_uri = "https://evil.example.test/"
    with pytest.raises(DownstreamContentError, match="URI"):
        _read_service(tmp_path, client).execute(_read("read_site", {}))
