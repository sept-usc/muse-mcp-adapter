"""Administrator CLI, including the full local walkthrough against a real simple MCP server."""

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.testclient import TestClient

from muse_mcp_adapter.application.serve import build_application, load_serve_config
from muse_mcp_adapter.cli import _parser, main  # pyright: ignore[reportPrivateUsage]
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from tests.fixtures.live_server import run_asgi

_LOCAL = ["--allow-http", "--allow-private-network"]
_NOW = "2026-10-05T10:00:00Z"


class Cli:
    def __init__(self, database: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self.database = database
        self.capsys = capsys

    def run(self, *arguments: str, flags: list[str] | None = None) -> tuple[int, Any, Any]:
        code = main(
            ["--database", str(self.database), "--actor", "tester", *(flags or []), *arguments]
        )
        captured = self.capsys.readouterr()
        out = json.loads(captured.out) if captured.out.strip() else None
        err = json.loads(captured.err) if captured.err.strip() else None
        return code, out, err

    def ok(self, *arguments: str, flags: list[str] | None = None) -> Any:
        code, out, err = self.run(*arguments, flags=flags)
        assert code == 0, err
        return out


def _simple_server() -> FastMCP:
    server = FastMCP(
        "Simple",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=["127.0.0.1:*", "localhost:*"]
        ),
    )

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def find_issue(issue_id: str) -> dict[str, str]:
        return {"id": issue_id, "title": "Example"}

    @server.tool()
    def create_issue(title: str) -> dict[str, str]:
        return {"id": "ISSUE-1", "title": title}

    @server.resource("mcp://workspace/guide", name="workspace_guide", mime_type="text/plain")
    def guide() -> str:
        return "Workspace guide"

    return server


@pytest.fixture
def cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Cli:
    return Cli(tmp_path / "adapter.db", capsys)


def test_operator_walkthrough_from_register_to_a_working_muse_call(
    tmp_path: Path, cli: Cli
) -> None:
    config_path = tmp_path / "serve.json"
    config_path.write_text(
        json.dumps(
            {
                "publicUrl": "http://127.0.0.1:8123",
                "allowInsecureHttp": True,
                "allowPrivateNetworks": True,
            }
        )
    )
    with run_asgi(_simple_server().streamable_http_app()) as port:
        endpoint = f"http://127.0.0.1:{port}/mcp"

        # By default a local plain-HTTP server is refused; development flags opt in explicitly.
        refused = cli.run(
            "server-register",
            "--server-id",
            "simple",
            "--display-name",
            "Simple",
            "--endpoint",
            endpoint,
            "--auth-profile",
            "none",
        )
        assert refused[0] == 1
        cli.ok(
            "server-register",
            "--server-id",
            "simple",
            "--display-name",
            "Simple",
            "--endpoint",
            endpoint,
            "--auth-profile",
            "none",
            flags=_LOCAL,
        )

        discovered = cli.ok("discover-remote", "--server-id", "simple", flags=_LOCAL)
        assert discovered["primitiveCount"] == 3

        draft = cli.ok("policy-template", "--server-id", "simple", "--expose-reads")
        by_key = {item["primitiveKey"]: item for item in draft}
        assert by_key["tool:find_issue"]["allowed"] is True
        assert by_key["resource:workspace_guide"]["allowed"] is True
        assert by_key["tool:create_issue"]["allowed"] is False  # writes start switched off
        assert by_key["tool:create_issue"]["classification"] == "write"
        by_key["tool:create_issue"]["allowed"] = True  # the owner reviews and enables it
        policies = tmp_path / "policies.json"
        policies.write_text(json.dumps(list(by_key.values())))

        cli.ok(
            "publish",
            "--server-id",
            "simple",
            "--catalog-version",
            "1.0.0",
            "--policies",
            str(policies),
        )
        cli.ok("activate", "--server-id", "simple", "--catalog-version", "1.0.0")
        assert cli.ok("drift", "--server-id", "simple", flags=_LOCAL) == []

        linked = cli.ok(
            "link",
            "--server-id",
            "simple",
            "--label",
            "My Muse",
            "--credential",
            "none",
            flags=["--config", str(config_path)],
        )
        assert linked["token"].startswith("mcpa_")
        spec_url = linked["connectorUrl"]
        assert spec_url.startswith("http://127.0.0.1:8123/c/") and spec_url.endswith(
            "/openapi.json"
        )
        assert spec_url in linked["museInstructions"]
        assert linked["token"] in linked["museInstructions"]
        assert linked["connectionCode"] in linked["museInstructions"]
        assert len(linked["connectionCode"].replace("-", "")) == 12

        listing = cli.ok("links", flags=["--config", str(config_path)])
        assert len(listing) == 1 and "token" not in listing[0]
        assert linked["token"] not in json.dumps(listing)
        assert linked["connectionCode"] not in json.dumps(listing)

        reissued = cli.ok(
            "link-code",
            "--connection-id",
            linked["connectionId"],
            flags=["--config", str(config_path)],
        )
        assert reissued["connectionCode"] != linked["connectionCode"]

        # Run the service exactly as `serve` would and call it the way Muse does.
        config = load_serve_config(config_path)
        with SqliteControlPlaneRepository(cli.database) as repository:
            app = build_application(repository, config, {}, database=cli.database)
            with TestClient(app) as http:
                slug = spec_url.split("/c/")[1].split("/")[0]
                headers = {"Authorization": f"Bearer {linked['token']}"}

                read = http.post(
                    f"/c/{slug}/operations/find_issue",
                    json={"issue_id": "ISSUE-7"},
                    headers=headers,
                )
                assert read.status_code == 200, read.text
                assert read.json()["structuredContent"] == {"id": "ISSUE-7", "title": "Example"}

                write = http.post(
                    f"/c/{slug}/operations/create_issue",
                    json={"title": "From Muse"},
                    headers=headers,
                )
                assert write.status_code == 200, write.text
                assert write.json()["structuredContent"]["title"] == "From Muse"

                guide = http.post(f"/c/{slug}/operations/workspace_guide", headers=headers)
                assert guide.status_code == 200 and "Workspace guide" in guide.text

        # Rotating invalidates the old token; unlinking stops everything.
        rotated = cli.ok(
            "rotate-token",
            "--connection-id",
            linked["connectionId"],
            flags=["--config", str(config_path)],
        )
        assert rotated["token"] != linked["token"]
        cli.ok(
            "unlink",
            "--connection-id",
            linked["connectionId"],
            flags=["--config", str(config_path)],
        )

        trail = [row["action"] for row in _audit(cli.database)]
        assert {
            "server.register",
            "server.discover",
            "catalog.publish",
            "catalog.activate",
            "connection.link",
            "connection.rotate_token",
            "connection.unlink",
        } <= set(trail)
        assert all(row["actor"] == "tester" for row in _audit(cli.database))


def _audit(database: Path) -> list[dict[str, object]]:
    with SqliteControlPlaneRepository(database) as repository:
        return [dict(row) for row in repository.list_admin_audit()]


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://mcp.example.test/mcp",
        "https://127.0.0.1/mcp",
        "https://2130706433/mcp",
        "https://user:secret@mcp.example.test/mcp",
    ],
)
def test_registration_rejects_unsafe_endpoints(cli: Cli, endpoint: str) -> None:
    code, out, err = cli.run(
        "server-register",
        "--server-id",
        "project-system",
        "--display-name",
        "P",
        "--endpoint",
        endpoint,
        "--auth-profile",
        "bearer_token",
    )
    assert code == 1 and out is None and err is not None
    assert cli.ok("server-list") == []


def test_servers_can_be_disabled_and_reenabled(cli: Cli) -> None:
    cli.ok(
        "server-register",
        "--server-id",
        "project-system",
        "--display-name",
        "P",
        "--endpoint",
        "https://mcp.example.test/mcp",
        "--auth-profile",
        "bearer_token",
    )
    assert cli.ok("server-disable", "--server-id", "project-system")["enabled"] is False
    assert cli.ok("server-enable", "--server-id", "project-system")["enabled"] is True
    code, _, err = cli.run("server-disable", "--server-id", "missing-server")
    assert code == 1 and err["error"] == "EntityNotFoundError"


def test_link_validates_credentials_and_oauth_connections_get_a_sign_in_link(
    tmp_path: Path, cli: Cli
) -> None:
    config = tmp_path / "serve.json"
    config.write_text(json.dumps({"publicUrl": "https://adapter.example.com"}))
    flags = ["--config", str(config)]
    cli.ok(
        "server-register",
        "--server-id",
        "project-system",
        "--display-name",
        "P",
        "--endpoint",
        "https://mcp.example.test/mcp",
        "--auth-profile",
        "oauth",
    )

    bad = cli.run(
        "link",
        "--server-id",
        "project-system",
        "--label",
        "x",
        "--credential",
        "env_token",
        flags=flags,
    )
    assert bad[0] == 1  # an environment variable name is required

    oauth = cli.ok(
        "link",
        "--server-id",
        "project-system",
        "--label",
        "Linear",
        "--credential",
        "oauth",
        "--scopes",
        "read, write",
        flags=flags,
    )
    assert oauth["scopes"] == ["read", "write"]
    assert oauth["loginUrl"].startswith("https://adapter.example.com/connect/")

    again = cli.ok("login", "--connection-id", oauth["connectionId"], flags=flags)
    assert again["loginUrl"] != oauth["loginUrl"]

    static = cli.ok(
        "link",
        "--server-id",
        "project-system",
        "--label",
        "Static",
        "--credential",
        "none",
        flags=flags,
    )
    refused = cli.run("login", "--connection-id", static["connectionId"], flags=flags)
    assert refused[0] == 1


def test_reconcile_settles_an_unknown_write(tmp_path: Path, cli: Cli) -> None:
    cli.ok(
        "server-register",
        "--server-id",
        "project-system",
        "--display-name",
        "P",
        "--endpoint",
        "https://mcp.example.test/mcp",
        "--auth-profile",
        "bearer_token",
    )
    from muse_mcp_adapter.domain.models import OperationRecord

    with SqliteControlPlaneRepository(cli.database) as repository:
        repository.create_operation(
            OperationRecord(
                "op-1",
                "r",
                "project-system",
                "1",
                "create_issue",
                "owner",
                "c",
                "write",
                "UNKNOWN",
                "k",
                "sha256:x",
                None,
                _NOW,
                _NOW,
                None,
                None,
                None,
                False,
            )
        )
    done = cli.ok("reconcile", "--operation-id", "op-1", "--outcome", "not_applied")
    assert done == {"operationId": "op-1", "status": "FAILED"}
    code, _, err = cli.run("reconcile", "--operation-id", "op-1", "--outcome", "completed")
    assert code == 1 and "response" in str(err)


def test_serve_requires_a_config_and_reports_misconfiguration(
    tmp_path: Path, cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _parser().parse_args(["serve"]).command == "serve"
    monkeypatch.delenv("MUSE_ADAPTER_CONFIG", raising=False)
    code, _, err = cli.run("serve")
    assert code == 1 and "--config" in str(err)

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"listen": {"host": "0.0.0.0"}}))
    code, _, err = cli.run("serve", flags=["--config", str(bad)])
    assert code == 1 and "publicUrl" in str(err)
