"""Service configuration, secret handling and one-time sign-in links."""

import json
import stat
from pathlib import Path

import pytest

from muse_mcp_adapter.application.serve import (
    build_application,
    load_serve_config,
    open_oauth_backend,
    open_repository,
)
from muse_mcp_adapter.domain.errors import EntityNotFoundError, InvalidStateError
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository


def _config(tmp_path: Path, **values: object) -> Path:
    path = tmp_path / "serve.json"
    path.write_text(json.dumps(values))
    return path


def test_defaults_are_safe_for_a_local_start(tmp_path: Path) -> None:
    config = load_serve_config(_config(tmp_path))
    assert config.public_url == "http://127.0.0.1:8080"
    assert config.approval_mode == "muse" and not config.allow_private_networks
    assert not config.allow_insecure_http


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"listen": {"host": "0.0.0.0"}}, "publicUrl is required"),
        ({"publicUrl": "http://adapter.example.com"}, "HTTPS"),
        ({"publicUrl": "https://adapter.example.com/path"}, "origin"),
        ({"publicUrl": "ftp://adapter.example.com"}, "origin"),
        ({"publicUrl": "https://a.example.com", "approvalMode": "maybe"}, "approvalMode"),
        ({"publicUrl": "https://a.example.com", "approvalMode": "signed"}, "approvalSecretEnv"),
        ({"publicUrl": "https://a.example.com", "limits": {"rateLimit": 0}}, "rateLimit"),
    ],
)
def test_invalid_configuration_is_rejected(
    tmp_path: Path, values: dict[str, object], message: str
) -> None:
    with pytest.raises(InvalidStateError, match=message):
        load_serve_config(_config(tmp_path, **values))


def test_plain_http_is_allowed_only_locally_or_by_explicit_opt_in(tmp_path: Path) -> None:
    assert load_serve_config(_config(tmp_path, publicUrl="http://localhost:9000")).public_url == (
        "http://localhost:9000"
    )
    opted_in = load_serve_config(
        _config(tmp_path, publicUrl="http://adapter.internal", allowInsecureHttp=True)
    )
    assert opted_in.allow_insecure_http


def test_oauth_key_file_is_private_and_reused(tmp_path: Path) -> None:
    database = tmp_path / "adapter.db"
    first = open_oauth_backend(database)
    key_file = database.with_suffix(".key")
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    key = key_file.read_bytes()

    open_oauth_backend(database)
    assert key_file.read_bytes() == key  # a second start must not rotate the key
    del first


def test_signed_mode_and_oauth_client_secrets_come_from_the_environment(tmp_path: Path) -> None:
    config = load_serve_config(
        _config(
            tmp_path,
            publicUrl="https://a.example.com",
            approvalMode="signed",
            approvalSecretEnv="APPROVAL",
            oauthClients={"linear": {"clientId": "cid", "clientSecretEnv": "LINEAR_SECRET"}},
        )
    )
    database = tmp_path / "adapter.db"
    with SqliteControlPlaneRepository(database) as repository:
        repository.initialize()
        with pytest.raises(InvalidStateError, match="LINEAR_SECRET"):
            build_application(repository, config, {"APPROVAL": "x" * 40}, database=database)
        with pytest.raises(InvalidStateError, match="APPROVAL"):
            build_application(repository, config, {"LINEAR_SECRET": "s"}, database=database)
        app = build_application(
            repository, config, {"APPROVAL": "x" * 40, "LINEAR_SECRET": "s"}, database=database
        )
        assert app is not None
    assert (tmp_path / "artifacts").is_dir()


def test_integrity_key_is_applied_when_configured(tmp_path: Path) -> None:
    config = load_serve_config(
        _config(tmp_path, publicUrl="https://a.example.com", integrityKeyEnv="CATALOG_KEY")
    )
    with pytest.raises(InvalidStateError, match="CATALOG_KEY"):
        open_repository(tmp_path / "a.db", config, {})
    env = {"CATALOG_KEY": "integrity-key-integrity-key-integrity"}
    with open_repository(tmp_path / "a.db", config, env) as repository:
        repository.initialize()


def test_sign_in_links_are_single_use_and_expire(tmp_path: Path) -> None:
    from muse_mcp_adapter.application.control_plane_service import ControlPlaneService
    from muse_mcp_adapter.connections import ConnectionService
    from muse_mcp_adapter.transport.security import EndpointSecurityPolicy

    with SqliteControlPlaneRepository(tmp_path / "a.db") as repository:
        repository.initialize()
        ControlPlaneService(
            repository, endpoint_policy=EndpointSecurityPolicy.local_development()
        ).register_server(
            server_id="simple",
            display_name="S",
            endpoint="http://127.0.0.1:1/mcp",
            auth_profile="oauth",
        )
        connection = (
            ConnectionService(repository)
            .link(server_id="simple", label="o", credential_kind="oauth")
            .record.connection_id
        )
        repository.create_login_nonce(
            "h1", connection, "2026-10-05T10:00:00Z", "2026-10-05T10:15:00Z"
        )

        assert repository.consume_login_nonce("h1", "2026-10-05T10:01:00Z") == (connection, None)
        with pytest.raises(EntityNotFoundError):  # spent
            repository.consume_login_nonce("h1", "2026-10-05T10:02:00Z")
        repository.create_login_nonce(
            "h2", connection, "2026-10-05T10:00:00Z", "2026-10-05T10:15:00Z"
        )
        with pytest.raises(EntityNotFoundError):  # expired
            repository.consume_login_nonce("h2", "2026-10-05T10:16:00Z")
        with pytest.raises(EntityNotFoundError):  # unknown
            repository.consume_login_nonce("nope", "2026-10-05T10:01:00Z")
