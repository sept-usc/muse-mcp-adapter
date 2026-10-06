"""The one-step sample setup used for live testing with Muse."""

import json
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from muse_mcp_adapter.application.serve import build_application, load_serve_config
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from tests.fixtures.live_server import run_asgi

sys.path.insert(0, str(Path(__file__).parents[2] / "examples"))
from prepare_sample_connector import main as prepare  # noqa: E402
from sample_mcp_server import build_server  # noqa: E402


def test_setup_produces_a_connector_that_works_and_can_be_rerun(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workdir = tmp_path / "e2e"
    with run_asgi(build_server().streamable_http_app()) as port:
        arguments = [
            "--public-url", "https://adapter.example.trycloudflare.com/",
            "--mcp-url", f"http://127.0.0.1:{port}/mcp",
            "--workdir", str(workdir),
        ]  # fmt: skip
        assert prepare(arguments) == 0
        printed = capsys.readouterr().out
        assert "Tell Muse" in printed and "serve" in printed

        link = json.loads((workdir / "link.json").read_text())
        assert link["connectorUrl"].startswith("https://adapter.example.trycloudflare.com/c/")

        config = load_serve_config(workdir / "serve.json")
        assert config.public_url == "https://adapter.example.trycloudflare.com"
        database = workdir / "adapter.db"
        with SqliteControlPlaneRepository(database) as repository:
            app = build_application(repository, config, {}, database=database)
            with TestClient(app) as http:
                base = link["connectorUrl"].removesuffix("/openapi.json").split(".com")[1]
                token = {"Authorization": f"Bearer {link['token']}"}
                found = http.post(
                    f"{base}/operations/find_issue", json={"issue_id": "ISSUE-1"}, headers=token
                )
                assert found.status_code == 200, found.text

        # running it again republishes instead of failing on the existing registration
        assert prepare(arguments) == 0
