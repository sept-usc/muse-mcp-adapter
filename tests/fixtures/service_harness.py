"""The Muse-facing service on a fake downstream, with a controllable clock."""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from muse_mcp_adapter.application.bootstrap import build_http_application
from muse_mcp_adapter.application.oauth_web import OAuthWebFlow
from muse_mcp_adapter.connections import (
    ConnectionCredentialResolver,
    ConnectionService,
    LinkedConnection,
)
from muse_mcp_adapter.data_plane.tool_client import McpReauthorizationRequired
from muse_mcp_adapter.domain.models import McpToolResult
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from muse_mcp_adapter.transport.oauth import InMemoryOAuthStorageBackend
from muse_mcp_adapter.transport.security import EndpointSecurityPolicy
from tests.fixtures.active_runtime import active_repository
from tests.fixtures.read_runtime import ConformanceReadClient
from tests.fixtures.tool_runtime import ConformanceToolClient

PUBLIC_URL = "https://adapter.example.com"


class Downstream(ConformanceToolClient, ConformanceReadClient):
    def __init__(self) -> None:
        ConformanceToolClient.__init__(self)
        ConformanceReadClient.__init__(self)
        self.reauthorize = False

    def call_tool(self, **kwargs: Any) -> McpToolResult:
        if self.reauthorize:
            raise McpReauthorizationRequired("The account must be authorized again.")
        return super().call_tool(**kwargs)


@dataclass
class Clock:
    now: datetime = field(default_factory=lambda: datetime(2026, 10, 5, 10, 0, tzinfo=UTC))

    def __call__(self) -> str:
        return self.now.isoformat().replace("+00:00", "Z")

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class Harness:
    http: TestClient
    repository: SqliteControlPlaneRepository
    connections: ConnectionService
    downstream: Downstream
    clock: Clock

    def link(self, label: str = "phone") -> LinkedConnection:
        return self.connections.link(
            server_id="project-system", label=label, credential_kind="none"
        )

    @staticmethod
    def auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def call(self, token: str, slug: str, operation: str = "find_issue") -> int:
        return self.http.post(
            f"/c/{slug}/operations/{operation}", json={"id": "ISSUE-1"}, headers=self.auth(token)
        ).status_code


@contextmanager
def harness(
    tmp_path: Path,
    *,
    rate_limit: int = 1000,
    with_oauth_flow: bool = False,
    clock: Callable[[], str] | None = None,
) -> Generator[Harness]:
    downstream = Downstream()
    the_clock = Clock()
    used = clock or the_clock
    with active_repository(tmp_path / "adapter.db") as repository:
        flow = (
            OAuthWebFlow(
                repository=repository,
                public_url=PUBLIC_URL,
                backend=InMemoryOAuthStorageBackend(),
                endpoint_policy=EndpointSecurityPolicy(),
                clock=used,
            )
            if with_oauth_flow
            else None
        )
        app = build_http_application(
            repository=repository,
            credential_resolver=ConnectionCredentialResolver(repository, {}),
            mcp_client=downstream,
            artifact_root=tmp_path / "artifacts",
            public_url=PUBLIC_URL,
            clock=used,
            oauth_flow=flow,
            rate_limit=rate_limit,
        )
        with TestClient(app, base_url=PUBLIC_URL) as http:
            yield Harness(
                http, repository, ConnectionService(repository, clock=used), downstream, the_clock
            )
