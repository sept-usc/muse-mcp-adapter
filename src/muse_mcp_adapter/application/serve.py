"""Deployment wiring for the Muse-facing HTTP ingress.

The config file only names environment variables; secret values are never stored in it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from starlette.applications import Starlette

from ..credentials.resolver import InMemoryCredentialResolver
from ..data_plane.approval import HmacApprovalVerifier
from ..data_plane.mcp_client import McpReadClient
from ..data_plane.tool_client import McpToolClient
from ..domain.errors import InvalidStateError
from ..persistence.sqlite_repository import SqliteControlPlaneRepository
from ..transport.sdk_client import McpSdkClient, StreamableHttpSessionFactory
from ..transport.security import EndpointSecurityPolicy
from .bootstrap import build_http_application
from .runtime import Principal


@dataclass(frozen=True, slots=True)
class IngressCaller:
    token_env: str
    identity: str
    servers: frozenset[str]
    connections: frozenset[str]
    administrator: bool


@dataclass(frozen=True, slots=True)
class CredentialBinding:
    identity: str
    connection: str
    server: str
    reference: str
    token_env: str
    scopes: frozenset[str]


@dataclass(frozen=True, slots=True)
class ServeConfig:
    artifact_root: Path
    approval_secret_env: str
    integrity_key_env: str | None
    callers: tuple[IngressCaller, ...]
    credentials: tuple[CredentialBinding, ...]
    rate_limit: int
    rate_window_seconds: float
    per_server_concurrency: int
    allow_private_networks: bool
    host: str
    port: int


def load_serve_config(path: Path) -> ServeConfig:
    raw: object = json.loads(path.read_text())
    config = _mapping(raw, "configuration")
    limits = _mapping(config.get("limits", {}), "limits")
    listen = _mapping(config.get("listen", {}), "listen")
    callers = tuple(
        IngressCaller(
            token_env=_string(item, "tokenEnv"),
            identity=_string(item, "identity"),
            servers=frozenset(_strings(item, "servers")),
            connections=frozenset(_strings(item, "connections")),
            administrator=item.get("administrator") is True,
        )
        for item in _mappings(config, "ingress")
    )
    credentials = tuple(
        CredentialBinding(
            identity=_string(item, "identity"),
            connection=_string(item, "connection"),
            server=_string(item, "server"),
            reference=_string(item, "ref"),
            token_env=_string(item, "tokenEnv"),
            scopes=frozenset(_strings(item, "scopes")),
        )
        for item in _mappings(config, "credentials")
    )
    if not callers:
        raise InvalidStateError("At least one ingress caller must be configured")
    return ServeConfig(
        artifact_root=Path(_string(config, "artifactRoot")),
        approval_secret_env=_string(config, "approvalSecretEnv"),
        integrity_key_env=cast(str | None, config.get("integrityKeyEnv")),
        callers=callers,
        credentials=credentials,
        rate_limit=_int(limits, "rateLimit", 60),
        rate_window_seconds=float(_int(limits, "rateWindowSeconds", 60)),
        per_server_concurrency=_int(limits, "perServerConcurrency", 8),
        allow_private_networks=config.get("allowPrivateNetworks") is True,
        host=cast(str, listen.get("host", "127.0.0.1")),
        port=_int(listen, "port", 8080),
    )


def open_repository(
    database: str | Path, config: ServeConfig, env: Mapping[str, str]
) -> SqliteControlPlaneRepository:
    key = None
    if config.integrity_key_env is not None:
        key = _secret(env, config.integrity_key_env).encode()
    return SqliteControlPlaneRepository(database, integrity_key=key)


def build_application(
    repository: SqliteControlPlaneRepository,
    config: ServeConfig,
    env: Mapping[str, str],
    *,
    mcp_client: McpReadClient | McpToolClient | None = None,
    clock: Callable[[], str] | None = None,
) -> Starlette:
    resolver = InMemoryCredentialResolver()
    for binding in config.credentials:
        resolver.register(
            local_identity_id=binding.identity,
            connection_id=binding.connection,
            server_id=binding.server,
            credential_ref=binding.reference,
            access_token=_secret(env, binding.token_env),
            scopes=binding.scopes,
        )
    tokens = {
        _secret(env, caller.token_env): Principal(
            local_identity_id=caller.identity,
            connection_ids=caller.connections,
            server_ids=caller.servers,
            administrator=caller.administrator,
        )
        for caller in config.callers
    }
    if len(tokens) != len(config.callers):
        raise InvalidStateError("Ingress callers must use distinct tokens")
    client = mcp_client or McpSdkClient(
        StreamableHttpSessionFactory(
            endpoint_policy=EndpointSecurityPolicy(
                allow_private_networks=config.allow_private_networks
            )
        )
    )
    config.artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return build_http_application(
        repository=repository,
        credential_resolver=resolver,
        mcp_client=client,
        approval_verifier=HmacApprovalVerifier(_secret(env, config.approval_secret_env).encode()),
        ingress_tokens=tokens,
        artifact_root=config.artifact_root,
        clock=clock or _utc_now,
        rate_limit=config.rate_limit,
        rate_window_seconds=config.rate_window_seconds,
        per_server_concurrency=config.per_server_concurrency,
    )


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


def _mappings(config: Mapping[str, object], key: str) -> tuple[Mapping[str, object], ...]:
    value = config.get(key, [])
    if not isinstance(value, list):
        raise InvalidStateError(f"{key} must be an array")
    return tuple(_mapping(item, key) for item in cast(Sequence[object], value))


def _string(config: Mapping[str, object], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value:
        raise InvalidStateError(f"{key} must be a non-empty string")
    return value


def _strings(config: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = config.get(key, [])
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in cast(Sequence[object], value)
    ):
        raise InvalidStateError(f"{key} must be an array of strings")
    return tuple(cast(Sequence[str], value))


def _int(config: Mapping[str, object], key: str, default: int) -> int:
    value = config.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise InvalidStateError(f"{key} must be a positive integer")
    return value
