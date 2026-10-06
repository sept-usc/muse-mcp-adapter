# pyright: basic
"""Test environment for the validation suite: an Atlas server and a connector in front of it."""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from starlette.testclient import TestClient
from validation_server import AtlasOptions, build_app
from validation_server.app import AtlasApp

from muse_mcp_adapter.application.control_plane_service import ControlPlaneService
from muse_mcp_adapter.application.policy_template import draft_policies
from muse_mcp_adapter.application.serve import ServeConfig, build_application
from muse_mcp_adapter.connections import ConnectionService, LinkedConnection
from muse_mcp_adapter.domain.models import DiscoverySnapshot, ResolvedCredential
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from muse_mcp_adapter.transport.sdk_client import McpSdkClient, StreamableHttpSessionFactory
from muse_mcp_adapter.transport.security import EndpointSecurityPolicy
from tests.fixtures.live_server import free_port, run_asgi
from tests.validation.direct import Direct

LOCAL = EndpointSecurityPolicy.local_development()
TOKEN_ENV = "ATLAS_TOKEN"


@dataclass
class AtlasHandle:
    atlas: AtlasApp
    options: AtlasOptions
    url: str

    @property
    def state(self) -> Any:
        return self.atlas.state

    def direct(self, **kwargs: Any) -> Direct:
        headers = (
            {"Authorization": f"Bearer {self.options.bearer_token}"}
            if self.options.auth == "bearer"
            else {}
        )
        return Direct(self.url, headers=headers, **kwargs)

    def control(self, path: str, payload: Mapping[str, Any] | None = None) -> Any:
        base = self.url.removesuffix("/mcp")
        if payload is None and path in ("state", "ledger"):
            return httpx.get(f"{base}/_control/{path}", timeout=10).json()
        return httpx.post(f"{base}/_control/{path}", json=payload or {}, timeout=10).json()

    def effects(self, tool: str | None = None) -> list[dict[str, Any]]:
        ledger = self.state.ledger()
        return [item for item in ledger if tool is None or item["tool"] == tool]


@contextmanager
def atlas_server(**options: Any) -> Generator[AtlasHandle]:
    port = free_port()
    settings = AtlasOptions(port=port, **options)
    app = build_app(settings)
    with run_asgi(app.asgi, port):
        yield AtlasHandle(app, settings, f"http://127.0.0.1:{port}/mcp")


@dataclass
class ConnectorHandle:
    http: TestClient
    repository: SqliteControlPlaneRepository
    connections: ConnectionService
    link: LinkedConnection
    spec: dict[str, Any]
    snapshot: DiscoverySnapshot
    policies: list[dict[str, Any]]

    @property
    def slug(self) -> str:
        return self.link.record.slug

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.link.token}"}

    def call(self, operation: str, body: Mapping[str, Any] | None = None, **headers: str) -> Any:
        return self.http.post(
            f"/c/{self.slug}/operations/{operation}",
            json=dict(body) if body is not None else None,
            headers={**self.headers, **headers},
        )

    def operations(self) -> dict[str, dict[str, Any]]:
        return {
            path.rsplit("/", 1)[-1]: item["post"]
            for path, item in self.spec["paths"].items()
            if path.startswith("/operations/")
        }

    def status(self, operation_id: str) -> Any:
        return self.http.get(f"/c/{self.slug}/executions/{operation_id}", headers=self.headers)


def _discovery_credential(handle: AtlasHandle) -> ResolvedCredential | None:
    if handle.options.auth != "bearer":
        return None
    return ResolvedCredential(
        credential_ref="validation://discovery",
        access_token=handle.options.bearer_token,
        local_identity_id="administrative-discovery",
        connection_id="administrative-discovery",
        server_id="atlas",
    )


@contextmanager
def connector(
    tmp_path: Path,
    handle: AtlasHandle,
    *,
    adjust: Callable[[list[dict[str, Any]]], None] | None = None,
    rate_limit: int = 100_000,
    publish_version: str = "1.0.0",
) -> Generator[ConnectorHandle]:
    """Publish a policy exposing everything Atlas offers and link a connection to it."""
    database = tmp_path / "adapter.db"
    with SqliteControlPlaneRepository(database) as repository:
        repository.initialize()
        control = ControlPlaneService(repository, endpoint_policy=LOCAL)
        control.register_server(
            server_id="atlas",
            display_name="Atlas",
            endpoint=handle.url,
            auth_profile=handle.options.auth,
        )
        client = McpSdkClient(
            StreamableHttpSessionFactory(endpoint_policy=LOCAL),
            discovery_credential=_discovery_credential(handle),
        )
        snapshot = control.discover("atlas", client)
        policies = draft_policies(snapshot, expose_reads=True)
        for policy in policies:
            policy["allowed"] = True  # the validation owner exposes everything
        if adjust is not None:
            adjust(policies)
        control.publish_catalog(
            server_id="atlas", catalog_version=publish_version, policies=policies
        )
        control.activate_catalog("atlas", publish_version)

        connections = ConnectionService(repository)
        credential = "env_token" if handle.options.auth == "bearer" else "none"
        link = connections.link(
            server_id="atlas",
            label="Validation",
            credential_kind=credential,
            credential_ref=TOKEN_ENV if credential == "env_token" else "",
        )
        config = ServeConfig(
            public_url="http://127.0.0.1:8123",
            allow_insecure_http=True,
            allow_private_networks=True,
            rate_limit=rate_limit,
        )
        env = {TOKEN_ENV: handle.options.bearer_token}
        app = build_application(repository, config, env, database=database)
        with TestClient(app, base_url="http://127.0.0.1:8123") as http:
            spec = http.get(f"/c/{link.record.slug}/openapi.json").json()
            yield ConnectorHandle(http, repository, connections, link, spec, snapshot, policies)
