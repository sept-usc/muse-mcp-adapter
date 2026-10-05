"""Administrator CLI behaviour added by the review fixes."""

import json
from pathlib import Path

import pytest

from muse_mcp_adapter.cli import _parser, main  # pyright: ignore[reportPrivateUsage]


def _run(
    capsys: pytest.CaptureFixture[str], database: Path, *arguments: str
) -> tuple[int, dict[str, object] | None, dict[str, object] | None]:
    code = main(["--database", str(database), *arguments])
    captured = capsys.readouterr()
    out = json.loads(captured.out) if captured.out.strip() else None
    err = json.loads(captured.err) if captured.err.strip() else None
    return code, out, err


def _register(
    capsys: pytest.CaptureFixture[str], database: Path, endpoint: str
) -> tuple[int, dict[str, object] | None, dict[str, object] | None]:
    return _run(
        capsys,
        database,
        "server-register",
        "--server-id",
        "project-system",
        "--display-name",
        "Project System",
        "--endpoint",
        endpoint,
        "--auth-profile",
        "bearer_token",
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://mcp.example.test/mcp",
        "https://127.0.0.1/mcp",
        "https://2130706433/mcp",
        "https://user:secret@mcp.example.test/mcp",
    ],
)
def test_registration_rejects_unsafe_endpoints(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], endpoint: str
) -> None:
    code, out, err = _register(capsys, tmp_path / "a.db", endpoint)

    assert code == 1 and out is None and err is not None
    assert err["error"] == "InvalidStateError"
    _, listing, _ = _run(capsys, tmp_path / "a.db", "server-list")
    assert listing == []


def test_servers_can_be_disabled_and_reenabled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "a.db"
    assert _register(capsys, database, "https://mcp.example.test/mcp")[0] == 0

    _, disabled, _ = _run(capsys, database, "server-disable", "--server-id", "project-system")
    assert isinstance(disabled, dict) and disabled["enabled"] is False
    _, enabled, _ = _run(capsys, database, "server-enable", "--server-id", "project-system")
    assert isinstance(enabled, dict) and enabled["enabled"] is True

    code, _, err = _run(capsys, database, "server-disable", "--server-id", "missing-server")
    assert code == 1 and err is not None and err["error"] == "EntityNotFoundError"


def test_serve_command_requires_a_config_and_reports_missing_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _parser().parse_args(["serve", "--config", "serve.json"]).command == "serve"

    config = tmp_path / "serve.json"
    config.write_text(
        json.dumps(
            {
                "artifactRoot": str(tmp_path / "artifacts"),
                "approvalSecretEnv": "ABSENT_APPROVAL_SECRET",
                "ingress": [
                    {
                        "tokenEnv": "ABSENT_TOKEN",
                        "identity": "user-1",
                        "servers": ["project-system"],
                        "connections": ["connection-1"],
                    }
                ],
            }
        )
    )
    monkeypatch.delenv("ABSENT_TOKEN", raising=False)
    code, _, err = _run(capsys, tmp_path / "a.db", "serve", "--config", str(config))
    assert code == 1 and err is not None
    assert "ABSENT_TOKEN" in str(err["message"])
