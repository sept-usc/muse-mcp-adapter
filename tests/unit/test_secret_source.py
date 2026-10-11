from pathlib import Path

import pytest

from muse_mcp_adapter.application.secret_source import MAX_SECRET_BYTES, SecretSource
from muse_mcp_adapter.application.serve import load_serve_config


def test_the_environment_wins_over_a_file(tmp_path: Path) -> None:
    (tmp_path / "TOKEN").write_text("from-file")
    source = SecretSource({"TOKEN": "from-env"}, tmp_path)
    assert source["TOKEN"] == "from-env"


def test_a_file_in_the_directory_is_used_when_the_variable_is_unset(tmp_path: Path) -> None:
    (tmp_path / "LINEAR_TOKEN").write_text("lin_secret\n")
    source = SecretSource({}, tmp_path)
    assert source.get("LINEAR_TOKEN") == "lin_secret"  # the trailing newline is not part of it
    assert "LINEAR_TOKEN" in source


def test_a_secret_added_later_is_found_without_restarting(tmp_path: Path) -> None:
    source = SecretSource({}, tmp_path)
    assert source.get("LATER") is None
    (tmp_path / "LATER").write_text("now-here")
    assert source.get("LATER") == "now-here"


def test_without_a_directory_it_behaves_like_the_environment() -> None:
    source = SecretSource({"A": "1"})
    assert source.get("A") == "1" and source.get("B") is None


@pytest.mark.parametrize(
    "name", ["../outside", "a/b", "..", ".hidden", "with space", "1starts", ""]
)
def test_names_that_could_leave_the_directory_are_refused(tmp_path: Path, name: str) -> None:
    (tmp_path.parent / "outside").write_text("stolen")
    assert SecretSource({}, tmp_path).get(name) is None


def test_a_link_pointing_outside_the_directory_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("stolen")
    directory = tmp_path / "secrets"
    directory.mkdir()
    (directory / "LEAK").symlink_to(outside)
    assert SecretSource({}, directory).get("LEAK") is None


def test_a_link_inside_the_directory_is_followed(tmp_path: Path) -> None:
    """Kubernetes mounts secrets as links into a hidden folder inside the directory."""
    (tmp_path / "..data").mkdir()
    (tmp_path / "..data" / "KEY").write_text("value")
    (tmp_path / "KEY").symlink_to(tmp_path / "..data" / "KEY")
    assert SecretSource({}, tmp_path).get("KEY") == "value"


def test_empty_oversized_and_binary_files_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "EMPTY").write_text("\n")
    (tmp_path / "HUGE").write_text("x" * (MAX_SECRET_BYTES + 1))
    (tmp_path / "BINARY").write_bytes(b"\xff\xfe\x00\x01")
    source = SecretSource({}, tmp_path)
    assert [source.get(n) for n in ("EMPTY", "HUGE", "BINARY")] == [None, None, None]


def test_it_lists_both_kinds_of_names(tmp_path: Path) -> None:
    (tmp_path / "FROM_FILE").write_text("v")
    source = SecretSource({"FROM_ENV": "v"}, tmp_path)
    assert set(source) == {"FROM_ENV", "FROM_FILE"} and len(source) == 2


def test_the_secrets_directory_is_a_serve_setting(tmp_path: Path) -> None:
    path = tmp_path / "serve.json"
    path.write_text('{"publicUrl": "https://adapter.example.com", "secretsDir": "/run/secrets"}')
    assert load_serve_config(path).secrets_dir == Path("/run/secrets")


def test_a_connection_token_can_come_from_a_secrets_file(tmp_path: Path) -> None:
    from muse_mcp_adapter.application.control_plane_service import ControlPlaneService
    from muse_mcp_adapter.connections import ConnectionCredentialResolver, ConnectionService
    from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
    from muse_mcp_adapter.transport.security import EndpointSecurityPolicy

    repository = SqliteControlPlaneRepository(tmp_path / "a.db")
    repository.initialize()
    ControlPlaneService(
        repository, endpoint_policy=EndpointSecurityPolicy.local_development()
    ).register_server(
        server_id="tracker",
        display_name="Tracker",
        endpoint="http://127.0.0.1:9101/mcp",
        auth_profile="bearer",
    )
    linked = ConnectionService(repository).link(
        server_id="tracker", label="Muse", credential_kind="env_token", credential_ref="TRACKER_KEY"
    )
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "TRACKER_KEY").write_text("key-from-file\n")

    credential = ConnectionCredentialResolver(repository, SecretSource({}, secrets)).resolve(
        local_identity_id=linked.record.identity_id,
        connection_id=linked.record.connection_id,
        server_id="tracker",
        required_scopes=frozenset(),
    )
    assert credential.access_token == "key-from-file"
