"""Deployment wiring for the Muse-facing HTTP service.

The configuration file holds no secrets: it names environment variables. Connections (who may
call, and with which downstream credential) live in the database and are managed with the
``link`` commands. Provider OAuth tokens are kept encrypted next to the database.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette

from ..connections import ConnectionCredentialResolver
from ..data_plane.approval import HmacApprovalVerifier
from ..data_plane.mcp_client import McpReadClient
from ..data_plane.owner_gate import OwnerGate
from ..data_plane.tool_client import McpToolClient
from ..domain.errors import InvalidStateError
from ..persistence.owner_store import OwnerStore
from ..persistence.sqlite_repository import SqliteControlPlaneRepository
from ..transport.oauth import EncryptedFileOAuthStorageBackend
from ..transport.sdk_client import McpSdkClient, StreamableHttpSessionFactory
from ..transport.security import EndpointSecurityPolicy
from .approval_web import ApprovalWeb
from .bootstrap import build_http_application
from .oauth_web import OAuthClientSeed, OAuthWebFlow, runtime_auth_factory

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True, slots=True)
class OAuthClientSetting:
    """A pre-registered OAuth client for a provider that has no dynamic registration."""

    client_id: str
    client_secret_env: str | None = None


@dataclass(frozen=True, slots=True)
class ServeConfig:
    public_url: str
    host: str = "127.0.0.1"
    port: int = 8080
    artifact_root: Path | None = None
    secrets_dir: Path | None = None
    approval_mode: str = "muse"
    approval_secret_env: str | None = None
    integrity_key_env: str | None = None
    allow_private_networks: bool = False
    allow_insecure_http: bool = False
    rate_limit: int = 60
    rate_window_seconds: float = 60.0
    per_server_concurrency: int = 8
    idempotency_window_seconds: int = 30
    oauth_clients: Mapping[str, OAuthClientSetting] = field(
        default_factory=dict[str, OAuthClientSetting]
    )


def load_serve_config(path: Path) -> ServeConfig:
    config = _mapping(json.loads(path.read_text()), "configuration")
    limits = _mapping(config.get("limits", {}), "limits")
    listen = _mapping(config.get("listen", {}), "listen")
    host = cast(str, listen.get("host", "127.0.0.1"))
    port = _int(listen, "port", 8080)
    allow_http = config.get("allowInsecureHttp") is True

    public_url = config.get("publicUrl")
    if public_url is None:
        if host not in _LOOPBACK:
            raise InvalidStateError("publicUrl is required when listening on a non-local address")
        public_url = f"http://{host}:{port}"
    if not isinstance(public_url, str):
        raise InvalidStateError("publicUrl must be a string")
    parsed = urlsplit(public_url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise InvalidStateError("publicUrl must be an origin such as https://adapter.example.com")
    if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK and not allow_http:
        raise InvalidStateError(
            "publicUrl must use HTTPS (plain HTTP is allowed only for local addresses)"
        )

    mode = config.get("approvalMode", "muse")
    if mode not in ("muse", "signed", "owner"):
        raise InvalidStateError("approvalMode must be 'muse', 'signed' or 'owner'")
    approval_env = config.get("approvalSecretEnv")
    if mode == "signed" and not isinstance(approval_env, str):
        raise InvalidStateError("approvalMode 'signed' requires approvalSecretEnv")

    clients: dict[str, OAuthClientSetting] = {}
    for server_id, raw in _mapping(config.get("oauthClients", {}), "oauthClients").items():
        item = _mapping(raw, f"oauthClients.{server_id}")
        clients[server_id] = OAuthClientSetting(
            client_id=_string(item, "clientId"),
            client_secret_env=cast(str | None, item.get("clientSecretEnv")),
        )

    artifact_root = config.get("artifactRoot")
    secrets_dir = config.get("secretsDir")
    return ServeConfig(
        public_url=public_url.rstrip("/"),
        host=host,
        port=port,
        artifact_root=Path(artifact_root) if isinstance(artifact_root, str) else None,
        secrets_dir=Path(secrets_dir) if isinstance(secrets_dir, str) else None,
        approval_mode=cast(str, mode),
        approval_secret_env=cast(str | None, approval_env),
        integrity_key_env=cast(str | None, config.get("integrityKeyEnv")),
        allow_private_networks=config.get("allowPrivateNetworks") is True,
        allow_insecure_http=allow_http,
        rate_limit=_int(limits, "rateLimit", 60),
        rate_window_seconds=float(_int(limits, "rateWindowSeconds", 60)),
        per_server_concurrency=_int(limits, "perServerConcurrency", 8),
        idempotency_window_seconds=_int(config, "idempotencyWindowSeconds", 30),
        oauth_clients=clients,
    )


def endpoint_policy(config: ServeConfig) -> EndpointSecurityPolicy:
    return EndpointSecurityPolicy(
        require_tls=not config.allow_insecure_http,
        allow_private_networks=config.allow_private_networks,
    )


def open_repository(
    database: str | Path, config: ServeConfig, env: Mapping[str, str]
) -> SqliteControlPlaneRepository:
    key = None
    if config.integrity_key_env is not None:
        key = _secret(env, config.integrity_key_env).encode()
    return SqliteControlPlaneRepository(database, integrity_key=key)


def open_oauth_backend(database: str | Path) -> EncryptedFileOAuthStorageBackend:
    """Encrypted OAuth token store next to the database; its key file is created owner-only."""
    base = Path(database)
    key = _load_or_create_key(base.with_suffix(".key"))
    return EncryptedFileOAuthStorageBackend(base.with_suffix(".oauth"), key)


def build_oauth_flow(
    repository: SqliteControlPlaneRepository,
    config: ServeConfig,
    env: Mapping[str, str],
    *,
    database: str | Path,
    clock: Callable[[], str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> OAuthWebFlow:
    seeds = {
        server_id: OAuthClientSeed(
            client_id=setting.client_id,
            client_secret=(
                _secret(env, setting.client_secret_env) if setting.client_secret_env else None
            ),
        )
        for server_id, setting in config.oauth_clients.items()
    }
    return OAuthWebFlow(
        repository=repository,
        public_url=config.public_url,
        backend=open_oauth_backend(database),
        endpoint_policy=endpoint_policy(config),
        clock=clock or _utc_now,
        clients=seeds,
        transport=transport,
    )


def build_application(
    repository: SqliteControlPlaneRepository,
    config: ServeConfig,
    env: Mapping[str, str],
    *,
    database: str | Path,
    mcp_client: McpReadClient | McpToolClient | None = None,
    clock: Callable[[], str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    clock = clock or _utc_now
    policy = endpoint_policy(config)
    flow = build_oauth_flow(
        repository, config, env, database=database, clock=clock, transport=transport
    )
    client = mcp_client or McpSdkClient(
        StreamableHttpSessionFactory(
            endpoint_policy=policy,
            auth_factory=runtime_auth_factory(
                repository, open_oauth_backend(database), public_url=config.public_url
            ),
            transport=transport,
        )
    )
    verifier = (
        HmacApprovalVerifier(_secret(env, cast(str, config.approval_secret_env)).encode())
        if config.approval_mode == "signed"
        else None
    )
    owner_gate: OwnerGate | None = None
    approval_web: ApprovalWeb | None = None
    if config.approval_mode == "owner":
        repository.initialize()
        store = OwnerStore(repository)
        if not store.passcode_is_set():
            raise InvalidStateError(
                "approvalMode 'owner' needs an approval passcode first. Set it with: "
                "muse-mcp-adapter owner-passcode"
            )
        owner_gate = OwnerGate(repository, store)
        approval_web = ApprovalWeb(store=store, clock=clock, public_url=config.public_url)
    artifact_root = config.artifact_root or Path(database).parent / "artifacts"
    artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return build_http_application(
        repository=repository,
        credential_resolver=ConnectionCredentialResolver(repository, env),
        mcp_client=client,
        artifact_root=artifact_root,
        public_url=config.public_url,
        clock=clock,
        approval_verifier=verifier,
        owner_gate=owner_gate,
        approval_web=approval_web,
        oauth_flow=flow,
        rate_limit=config.rate_limit,
        rate_window_seconds=config.rate_window_seconds,
        per_server_concurrency=config.per_server_concurrency,
        idempotency_window_seconds=config.idempotency_window_seconds,
    )


def _load_or_create_key(path: Path) -> bytes:
    if path.exists():
        return path.read_bytes().strip()
    key = EncryptedFileOAuthStorageBackend.generate_key()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(key)
    return key


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _secret(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not value:
        raise InvalidStateError(f"Environment variable {name} is not set")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise InvalidStateError(f"{label} must be a JSON object")
    return cast(Mapping[str, object], value)


def _string(config: Mapping[str, object], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value:
        raise InvalidStateError(f"{key} must be a non-empty string")
    return value


def _int(config: Mapping[str, object], key: str, default: int) -> int:
    value = config.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise InvalidStateError(f"{key} must be a positive integer")
    return value
