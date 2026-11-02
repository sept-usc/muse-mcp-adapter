"""Chat-driven setup against the real application: an agent, an owner's browser, a fake server.

The agent is an HTTP client holding the setup connector's token. The owner is a second client with
its own cookies, the way a browser on another device would be. The MCP server behind the new
connector is fake, so what is checked is the adapter: what it exposes, to whom, and why.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sqlite3
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any

from starlette.testclient import TestClient

from muse_mcp_adapter.application.serve import build_application, load_serve_config
from muse_mcp_adapter.cli import main
from muse_mcp_adapter.discovery.client import ManifestDiscoveryClient
from muse_mcp_adapter.domain.models import McpContentBlock, McpToolResult, ResolvedCredential
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from tests.fixtures.service_harness import PUBLIC_URL, Clock

CSRF_COOKIE = "muse_adapter_owner_csrf"


def tracker_manifest(poison: str | None = None) -> dict[str, object]:
    """What a small issue tracker offers: two reads, a write, a destructive tool, a lookalike."""

    def tool(name: str, description: str, *, read_only: bool | None = None) -> dict[str, object]:
        item: dict[str, object] = {
            "type": "tool",
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "additionalProperties": True,
            },
        }
        if read_only is not None:
            item["annotations"] = {"readOnlyHint": read_only}
        return item

    return {
        "protocolVersion": "2025-11-25",
        "serverInfo": {"name": "Tracker"},
        "capabilities": {},
        "primitives": [
            tool("list_issues", "List issues.", read_only=True),
            tool("get_issue", poison or "Get one issue.", read_only=True),
            tool("create_issue", "Create an issue."),
            tool("delete_issue", "Delete an issue."),
            tool("search_notes", "Search the notes."),
        ],
    }


def projects_manifest() -> dict[str, object]:
    """A tracker whose operations take a project, so a user can say "only project ENG"."""

    def tool(
        name: str, properties: Mapping[str, object], *, read_only: bool | None = None
    ) -> dict[str, object]:
        item: dict[str, object] = {
            "type": "tool",
            "name": name,
            "description": f"{name} in a tracker.",
            "inputSchema": {"type": "object", "properties": dict(properties)},
        }
        if read_only is not None:
            item["annotations"] = {"readOnlyHint": read_only}
        return item

    text: dict[str, object] = {"type": "string"}
    return {
        "protocolVersion": "2025-11-25",
        "serverInfo": {"name": "Tracker"},
        "capabilities": {},
        "primitives": [
            tool("list_issues", {"project": text, "query": text}, read_only=True),
            tool("get_issue", {"project": text, "id": text}, read_only=True),
            tool("create_issue", {"project": text, "title": text}),
            tool("search_notes", {"query": text}, read_only=True),
        ],
    }


class FakeTools:
    """The MCP server behind a new connector: records what it is asked and who asked."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, object], str]] = []

    def call_tool(
        self,
        *,
        endpoint: str,
        name: str,
        arguments: Mapping[str, object],
        credential: ResolvedCredential,
        idempotency_key: str | None,
        timeout_seconds: int | None = None,
        progress_callback: Callable[[float, float | None, str | None], None] | None = None,
        elicitation_answer: Mapping[str, object] | None = None,
        cancellation: Event | None = None,
        log_callback: Callable[[str, str], None] | None = None,
    ) -> McpToolResult:
        del endpoint, idempotency_key, timeout_seconds, progress_callback
        del elicitation_answer, cancellation, log_callback
        self.calls.append((name, dict(arguments), credential.access_token))
        return McpToolResult(
            content=(McpContentBlock(block_type="text", text=f"{name} done"),),
            structured_content={"ok": True},
        )

    def read_resource(self, **_: object) -> object:
        raise AssertionError("no resources in this fixture")

    def get_prompt(self, **_: object) -> object:
        raise AssertionError("no prompts in this fixture")


class FakeDiscovery:
    """Stands in for connecting to the server: counts how often it is asked and with what."""

    def __init__(self, manifest: Mapping[str, object]) -> None:
        self.manifest = manifest
        self.credentials: list[ResolvedCredential | None] = []
        self.failure: Exception | None = None

    def __call__(self, credential: ResolvedCredential | None) -> ManifestDiscoveryClient:
        self.credentials.append(credential)
        if self.failure is not None:
            raise self.failure
        return ManifestDiscoveryClient(self.manifest, discovered_at="2026-10-05T10:00:30Z")


@dataclass
class ChatHarness:
    agent: TestClient
    browser: TestClient
    repository: SqliteControlPlaneRepository
    clock: Clock
    tools: FakeTools
    discovery: FakeDiscovery
    database: Path
    config_path: Path
    setup_token: str
    setup_slug: str
    owner_code: str

    def rows(self, statement: str) -> list[tuple[Any, ...]]:
        """Read the database the way an administrator with the file could."""
        with contextlib.closing(sqlite3.connect(self.database)) as raw:
            return raw.execute(statement).fetchall()

    # --- the agent ---------------------------------------------------------------------

    def ask(
        self, operation: str, body: Mapping[str, object] | None = None, *, token: str | None = None
    ) -> Any:
        return self.agent.post(
            f"/c/{self.setup_slug}/operations/{operation}",
            json=dict(body or {}),
            headers={"Authorization": f"Bearer {token or self.setup_token}"},
        )

    def propose(self, **fields: object) -> dict[str, Any]:
        body: dict[str, object] = {"server": "learn", "userWords": "connect it, read only"}
        body.update(fields)
        response = self.ask("propose_connection", body)
        assert response.status_code == 200, response.text
        return response.json()["structuredContent"]

    def status(self, proposal_id: str) -> dict[str, Any]:
        response = self.ask("get_proposal_status", {"proposalId": proposal_id})
        assert response.status_code == 200, response.text
        return response.json()["structuredContent"]

    # --- the owner's browser -----------------------------------------------------------

    def get(self, link_or_path: str) -> Any:
        return self.browser.get(_path(link_or_path), follow_redirects=False)

    def post(self, path: str, **fields: object) -> Any:
        csrf = self.browser.cookies.get(CSRF_COOKIE) or ""
        data: dict[str, Any] = {"csrf": csrf}
        data.update(fields)
        return self.browser.post(_path(path), data=data, follow_redirects=False)

    def unlock(self, link: str, code: str | None = None) -> Any:
        """Open the link, type the owner code. Returns the answer to the code."""
        self.get(link)
        return self.post(f"{_path(link)}/unlock", code=code or self.owner_code)

    def review(self, link: str, *, plan: str | None = None, secret: str | None = None) -> str:
        """Unlock, start, and return the review page's HTML."""
        assert self.unlock(link).status_code == 302
        page = self.get(link)
        assert page.status_code == 200, page.text
        fields: dict[str, object] = {}
        if plan:
            fields["plan"] = plan
        if secret:
            fields["secret"] = secret
        started = self.post(f"{_path(link)}/start", **fields)
        assert started.status_code == 302, started.text
        reviewed = self.get(link)
        assert reviewed.status_code == 200, reviewed.text
        return reviewed.text

    def approve(self, link: str, page: str, *, drop: tuple[str, ...] = ()) -> Any:
        keys = [key for key in ticked(page) if key not in drop]
        return self.post(
            f"{_path(link)}/decide",
            action="approve",
            digest=digest_of(page),
            chosen=keys,
        )


@contextmanager
def chat_harness(
    tmp_path: Path,
    *,
    manifest: Mapping[str, object] | None = None,
    serve_chat_setup: bool = True,
) -> Generator[ChatHarness]:
    database = tmp_path / "adapter.db"
    config_path = tmp_path / "serve.json"
    settings: dict[str, object] = {"publicUrl": PUBLIC_URL, "chatSetup": True}
    config_path.write_text(json.dumps(settings))
    enabled = run_cli(database, config_path, "chat-setup-enable")
    slug = re.search(r"/c/([^/]+)/openapi.json", str(enabled["connectorUrl"]))
    assert slug is not None
    clock = Clock()
    tools = FakeTools()
    discovery = FakeDiscovery(manifest or tracker_manifest())
    if not serve_chat_setup:  # enabled at the command line, then the service restarted without it
        config_path.write_text(json.dumps({"publicUrl": PUBLIC_URL, "chatSetup": False}))
    with SqliteControlPlaneRepository(database) as repository:
        repository.initialize()
        app = build_application(
            repository,
            load_serve_config(config_path),
            {},
            database=database,
            mcp_client=tools,  # type: ignore[arg-type]
            clock=clock,
            discovery=discovery,
        )
        with (
            TestClient(app, base_url=PUBLIC_URL) as agent,
            TestClient(app, base_url=PUBLIC_URL) as browser,
        ):
            yield ChatHarness(
                agent=agent,
                browser=browser,
                repository=repository,
                clock=clock,
                tools=tools,
                discovery=discovery,
                database=database,
                config_path=config_path,
                setup_token=str(enabled["token"]),
                setup_slug=slug.group(1),
                owner_code=str(enabled["ownerCode"]),
            )


def run_cli(database: Path, config_path: Path, *words: str) -> Any:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(["--database", str(database), "--config", str(config_path), *words])
    assert code == 0, out.getvalue()
    loaded: Any = json.loads(out.getvalue())
    return loaded


def _path(link_or_path: str) -> str:
    return re.sub(r"^https?://[^/]+", "", link_or_path)


def ticked(page: str) -> list[str]:
    """The values of the 'chosen' checkboxes that the page ticks by default."""
    return re.findall(r"name=chosen value='([^']+)' checked", page)


def digest_of(page: str) -> str:
    match = re.search(r"name=digest value='([^']+)'", page)
    assert match is not None, "no digest on the page"
    return match.group(1)


def connector_from(page: str) -> tuple[str, str]:
    """The connector address and token shown once on the done page."""
    address = re.search(r"Connector address:<br><code>([^<]+)</code>", page)
    token = re.search(r"Connector token \(shown only here\):<br><code>([^<]+)</code>", page)
    assert address is not None and token is not None, "the done page shows no connector"
    return address.group(1), token.group(1)
