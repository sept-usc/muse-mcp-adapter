import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from muse_mcp_adapter.cli import main
from tests.fixtures.phase_one import discovery_manifest, phase_one_fixture


def test_phase_two_cli_flow_persists_activates_and_rolls_back(tmp_path: Path, capsys: Any) -> None:
    database = tmp_path / "adapter.db"
    manifest_path = tmp_path / "manifest.json"
    policies_path = tmp_path / "policies.json"
    manifest = discovery_manifest()
    policies = phase_one_fixture()["policies"]
    manifest_path.write_text(json.dumps(manifest))
    policies_path.write_text(json.dumps(policies))

    assert (
        _run(
            capsys,
            database,
            "server-register",
            "--server-id",
            "project-system",
            "--display-name",
            "Project System",
            "--endpoint",
            "https://mcp.example.test/mcp",
            "--auth-profile",
            "delegated_oauth",
        )["serverId"]
        == "project-system"
    )

    assert (
        _run(
            capsys,
            database,
            "discover",
            "--server-id",
            "project-system",
            "--manifest",
            str(manifest_path),
        )["primitiveCount"]
        == 5
    )

    _run(
        capsys,
        database,
        "publish",
        "--server-id",
        "project-system",
        "--catalog-version",
        "1.0.0",
        "--policies",
        str(policies_path),
    )
    _run(
        capsys,
        database,
        "activate",
        "--server-id",
        "project-system",
        "--catalog-version",
        "1.0.0",
    )

    changed = deepcopy(manifest)
    changed_tool = next(item for item in changed["primitives"] if item["name"] == "find_issue")
    changed_tool["inputSchema"]["properties"]["teamId"] = {"type": "string"}
    manifest_path.write_text(json.dumps(changed))
    _run(
        capsys,
        database,
        "discover",
        "--server-id",
        "project-system",
        "--manifest",
        str(manifest_path),
    )
    drift = _run(
        capsys,
        database,
        "drift",
        "--server-id",
        "project-system",
    )
    assert any(
        event["primitiveKey"] == "tool:find_issue"
        and event["reason"] == "input_schema_changed"
        and event["severity"] == "block"
        for event in drift
    )
    _run(
        capsys,
        database,
        "publish",
        "--server-id",
        "project-system",
        "--catalog-version",
        "2.0.0",
        "--policies",
        str(policies_path),
    )
    _run(
        capsys,
        database,
        "activate",
        "--server-id",
        "project-system",
        "--catalog-version",
        "2.0.0",
    )

    rollback = _run(
        capsys,
        database,
        "rollback",
        "--server-id",
        "project-system",
    )
    status = _run(
        capsys,
        database,
        "status",
        "--server-id",
        "project-system",
    )

    assert rollback["eventType"] == "rollback"
    assert rollback["catalogVersion"] == "1.0.0"
    assert status["activeCatalog"]["catalogVersion"] == "1.0.0"
    assert status["latestDiscovery"]["primitiveCount"] == 5


def _run(capsys: Any, database: Path, *arguments: str) -> Any:
    exit_code = main(["--database", str(database), *arguments])
    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    return json.loads(captured.out)
