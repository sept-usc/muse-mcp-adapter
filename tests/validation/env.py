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
def atlas_server(*, port: int | None = None, **options: Any) -> Generator[AtlasHandle]:
    port = port or free_port()
    settings = AtlasOptions(port=port, **options)
    app = build_app(settings)
    with run_asgi(app.asgi, port):
        yield AtlasHandle(app, settings, f"http://127.0.0.1:{port}/mcp")


@dataclass
class ConnectorHandle:
    http: Any
    repository: SqliteControlPlaneRepository
    connections: ConnectionService
    link: LinkedConnection
    spec: dict[str, Any]
    snapshot: DiscoverySnapshot
    policies: list[dict[str, Any]]
    database: Path
    atlas: AtlasHandle

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
    per_server_concurrency: int = 8,
    publish_version: str = "1.0.0",
    live: bool = False,
    token_override: str | None = None,
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
            per_server_concurrency=per_server_concurrency,
        )
        env = {TOKEN_ENV: token_override or handle.options.bearer_token}
        if token_override == "":
            env = {}
        app = build_application(repository, config, env, database=database)
        if live:
            port = free_port()
            config = ServeConfig(
                public_url=f"http://127.0.0.1:{port}",
                allow_insecure_http=True,
                allow_private_networks=True,
                rate_limit=rate_limit,
                per_server_concurrency=per_server_concurrency,
            )
            app = build_application(repository, config, env, database=database)
            with run_asgi(app, port), httpx.Client(base_url=config.public_url, timeout=60) as real:
                spec = real.get(f"/c/{link.record.slug}/openapi.json").json()
                yield ConnectorHandle(
                    real,
                    repository,
                    connections,
                    link,
                    spec,
                    snapshot,
                    policies,
                    database,
                    handle,
                )
            return
        with TestClient(app, base_url="http://127.0.0.1:8123") as http:
            spec = http.get(f"/c/{link.record.slug}/openapi.json").json()
            yield ConnectorHandle(
                http, repository, connections, link, spec, snapshot, policies, database, handle
            )


def _service(connector_handle: ConnectorHandle) -> ControlPlaneService:
    return ControlPlaneService(connector_handle.repository, endpoint_policy=LOCAL)


def rediscover(connector_handle: ConnectorHandle) -> DiscoverySnapshot:
    """Run discovery again, as an owner does when checking for changes upstream."""
    client = McpSdkClient(
        StreamableHttpSessionFactory(endpoint_policy=LOCAL),
        discovery_credential=_discovery_credential(connector_handle.atlas),
    )
    return _service(connector_handle).discover("atlas", client)


def drift(connector_handle: ConnectorHandle) -> set[tuple[str, str, str]]:
    """The drift events as (primitive, severity, reason)."""
    return {
        (event.primitive_key, event.severity, event.reason)
        for event in _service(connector_handle).get_drift("atlas")
    }


def publish(
    connector_handle: ConnectorHandle, version: str, policies: list[dict[str, Any]]
) -> None:
    service = _service(connector_handle)
    service.publish_catalog(server_id="atlas", catalog_version=version, policies=policies)
    service.activate_catalog("atlas", version)


def rollback(connector_handle: ConnectorHandle) -> None:
    _service(connector_handle).rollback_catalog("atlas")
