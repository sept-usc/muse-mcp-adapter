"""A real adapter, a real OAuth provider and two HTTP clients, for chat setup over sockets.

The provider is a fake authorization server with a protected MCP server behind it. The adapter
runs as the real HTTP service. One client plays the agent, the other the owner's browser.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from muse_mcp_adapter.application.serve import build_application, load_serve_config
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from tests.fixtures.chat_setup import CSRF_COOKIE, run_cli
from tests.fixtures.fake_oauth import FakeAuthorizationServer, protected_server
from tests.fixtures.live_server import free_port, run_asgi


@dataclass
class World:
    adapter: str
    provider_endpoint: str
    provider: FakeAuthorizationServer
    agent: httpx.Client
    browser: httpx.Client
    setup_slug: str
    setup_token: str
    owner_code: str
    repository: SqliteControlPlaneRepository
    database: Path

    def ask(self, operation: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self.agent.post(
            f"{self.adapter}/c/{self.setup_slug}/operations/{operation}",
            json=body,
            headers={"Authorization": f"Bearer {self.setup_token}"},
        )
        assert response.status_code == 200, response.text
        return response.json()["structuredContent"]

    def rows(self, statement: str) -> list[tuple[Any, ...]]:
        """Read the database the way an administrator with the file could."""
        with contextlib.closing(sqlite3.connect(self.database)) as raw:
            return raw.execute(statement).fetchall()

    def post(self, url: str, **fields: object) -> httpx.Response:
        csrf = self.browser.cookies.get(CSRF_COOKIE) or ""
        return self.browser.post(url, data={"csrf": csrf, **fields}, follow_redirects=False)


@contextmanager
def world(tmp_path: Path, *, registration: bool = True) -> Generator[World]:
    provider = FakeAuthorizationServer()
    provider_port, adapter_port = free_port(), free_port()
    database, config_path = tmp_path / "adapter.db", tmp_path / "serve.json"
    adapter = f"http://127.0.0.1:{adapter_port}"
    config_path.write_text(
        json.dumps(
            {
                "publicUrl": adapter,
                "allowInsecureHttp": True,
                "allowPrivateNetworks": True,
                "chatSetup": True,
            }
        )
    )
    enabled = run_cli(database, config_path, "chat-setup-enable")
    slug = re.search(r"/c/([^/]+)/openapi.json", str(enabled["connectorUrl"]))
    assert slug is not None
    with SqliteControlPlaneRepository(database) as repository:
        repository.initialize()
        app = build_application(repository, load_serve_config(config_path), {}, database=database)
        server = protected_server(provider, provider_port, registration=registration)
        with (
            run_asgi(server.streamable_http_app(), provider_port),
            run_asgi(app, adapter_port),
            httpx.Client(timeout=30) as agent,
            httpx.Client(timeout=30) as browser,
        ):
            yield World(
                adapter=adapter,
                provider_endpoint=f"http://127.0.0.1:{provider_port}/mcp",
                provider=provider,
                agent=agent,
                browser=browser,
                setup_slug=slug.group(1),
                setup_token=str(enabled["token"]),
                owner_code=str(enabled["ownerCode"]),
                repository=repository,
                database=database,
            )
