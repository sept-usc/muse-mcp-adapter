import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from .application.control_plane_service import ControlPlaneService
from .catalog.serde import catalog_to_dict
from .discovery.client import ManifestDiscoveryClient
from .domain.errors import (
    CatalogValidationError,
    ControlPlaneError,
    DataPlaneError,
    EntityNotFoundError,
)
from .domain.models import (
    CatalogActivation,
    DiscoverySnapshot,
    DriftEvent,
    ResolvedCredential,
    ServerRegistration,
)
from .persistence.sqlite_repository import SqliteControlPlaneRepository
from .transport.sdk_client import McpSdkClient


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "serve":
            return _serve(args)
        with SqliteControlPlaneRepository(args.database) as repository:
            repository.initialize()
            service = ControlPlaneService(repository)
            result = _execute(args, service)
        if result is not None:
            print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (
        ControlPlaneError,
        DataPlaneError,
        CatalogValidationError,
        OSError,
        json.JSONDecodeError,
    ) as error:
        print(json.dumps(_error_payload(error), sort_keys=True), file=sys.stderr)
        return 1


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .application.serve import build_application, load_serve_config, open_repository

    config = load_serve_config(Path(args.config))
    with open_repository(args.database, config, os.environ) as repository:
        application = build_application(repository, config, os.environ)
        uvicorn.run(application, host=config.host, port=config.port, log_level="info")
    return 0


def _execute(args: argparse.Namespace, service: ControlPlaneService) -> object:
    if args.command == "server-register":
        server = service.register_server(
            server_id=args.server_id,
            display_name=args.display_name,
            endpoint=args.endpoint,
            auth_profile=args.auth_profile,
            provider_pack=args.provider_pack,
        )
        return _server_dict(server)

    if args.command == "server-list":
        return [_server_dict(server) for server in service.list_servers()]

    if args.command == "discover":
        manifest = _load_mapping(Path(args.manifest))
        snapshot = service.discover(args.server_id, ManifestDiscoveryClient(manifest))
        return _snapshot_dict(snapshot)

    if args.command == "discover-remote":
        token = os.environ.get(args.token_env) if args.token_env else None
        credential = (
            ResolvedCredential(
                credential_ref=f"environment://{args.token_env}",
                access_token=token,
                local_identity_id="administrative-discovery",
                connection_id="administrative-discovery",
                server_id=args.server_id,
            )
            if token is not None
            else None
        )
        snapshot = service.discover(
            args.server_id,
            McpSdkClient(discovery_credential=credential),
        )
        return _snapshot_dict(snapshot)

    if args.command in ("server-enable", "server-disable"):
        return _server_dict(
            service.set_server_enabled(
                args.server_id, enabled=args.command == "server-enable"
            )
        )

    if args.command == "publish":
        policies = _load_policy_list(Path(args.policies))
        catalog = service.publish_catalog(
            server_id=args.server_id,
            catalog_version=args.catalog_version,
            policies=policies,
        )
        return catalog_to_dict(catalog)

    if args.command == "activate":
        return _activation_dict(
            service.activate_catalog(args.server_id, args.catalog_version)
        )

    if args.command == "rollback":
        return _activation_dict(service.rollback_catalog(args.server_id))

    if args.command == "drift":
        return [_drift_dict(event) for event in service.get_drift(args.server_id)]

    if args.command == "status":
        server = next(
            (item for item in service.list_servers() if item.server_id == args.server_id),
            None,
        )
        if server is None:
            raise EntityNotFoundError(f"Server {args.server_id} was not found")
        result: dict[str, object] = {"server": _server_dict(server)}
        try:
            result["latestDiscovery"] = _snapshot_dict(
                service.get_latest_discovery(args.server_id)
            )
        except EntityNotFoundError:
            result["latestDiscovery"] = None
        try:
            result["activeCatalog"] = catalog_to_dict(
                service.get_active_catalog(args.server_id)
            )
        except EntityNotFoundError:
            result["activeCatalog"] = None
        return result

    raise AssertionError(f"Unhandled command {args.command}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="muse-mcp-adapter")
    parser.add_argument("--database", default="adapter.db")
    commands = parser.add_subparsers(dest="command", required=True)

    register = commands.add_parser("server-register")
    register.add_argument("--server-id", required=True)
    register.add_argument("--display-name", required=True)
    register.add_argument("--endpoint", required=True)
    register.add_argument("--auth-profile", required=True)
    register.add_argument("--provider-pack")

    commands.add_parser("server-list")

    serve = commands.add_parser(
        "serve", help="Run the Muse-facing HTTP ingress (secrets are read from the environment)"
    )
    serve.add_argument("--config", required=True)

    discover = commands.add_parser("discover")
    discover.add_argument("--server-id", required=True)
    discover.add_argument("--manifest", required=True)

    remote = commands.add_parser("discover-remote")
    remote.add_argument("--server-id", required=True)
    remote.add_argument(
        "--token-env",
        help="Name of an environment variable containing a bearer token",
    )

    enable = commands.add_parser("server-enable")
    enable.add_argument("--server-id", required=True)
    disable = commands.add_parser("server-disable")
    disable.add_argument("--server-id", required=True)

    publish = commands.add_parser("publish")
    publish.add_argument("--server-id", required=True)
    publish.add_argument("--catalog-version", required=True)
    publish.add_argument("--policies", required=True)

    activate = commands.add_parser("activate")
    activate.add_argument("--server-id", required=True)
    activate.add_argument("--catalog-version", required=True)

    rollback = commands.add_parser("rollback")
    rollback.add_argument("--server-id", required=True)

    drift = commands.add_parser("drift")
    drift.add_argument("--server-id", required=True)

    status = commands.add_parser("status")
    status.add_argument("--server-id", required=True)
    return parser


def _load_mapping(path: Path) -> Mapping[str, object]:
    value: object = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ControlPlaneError(f"{path} must contain a JSON object")
    mapping = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise ControlPlaneError(f"{path} must contain a JSON object")
    return cast(Mapping[str, object], mapping)


def _load_policy_list(path: Path) -> tuple[Mapping[str, object], ...]:
    value: object = json.loads(path.read_text())
    if not isinstance(value, list):
        raise ControlPlaneError(f"{path} must contain a JSON array")
    policies: list[Mapping[str, object]] = []
    for item in cast(list[object], value):
        if not isinstance(item, dict):
            raise ControlPlaneError(f"{path} contains an invalid policy")
        mapping = cast(dict[object, object], item)
        if not all(isinstance(key, str) for key in mapping):
            raise ControlPlaneError(f"{path} contains an invalid policy")
        policies.append(cast(Mapping[str, object], mapping))
    return tuple(policies)


def _server_dict(server: ServerRegistration) -> dict[str, object]:
    return {
        "serverId": server.server_id,
        "displayName": server.display_name,
        "endpoint": server.endpoint,
        "authProfile": server.auth_profile,
        "providerPack": server.provider_pack,
        "enabled": server.enabled,
        "createdAt": server.created_at,
        "updatedAt": server.updated_at,
    }


def _snapshot_dict(snapshot: DiscoverySnapshot) -> dict[str, object]:
    return {
        "snapshotId": snapshot.snapshot_id,
        "serverId": snapshot.server_id,
        "protocolVersion": snapshot.protocol_version,
        "discoveredAt": snapshot.discovered_at,
        "snapshotDigest": snapshot.snapshot_digest,
        "primitiveCount": len(snapshot.primitives),
    }


def _activation_dict(activation: CatalogActivation) -> dict[str, object]:
    return {
        "serverId": activation.server_id,
        "catalogVersion": activation.catalog_version,
        "previousCatalogVersion": activation.previous_catalog_version,
        "eventType": activation.event_type,
        "activatedAt": activation.activated_at,
    }


def _drift_dict(event: DriftEvent) -> dict[str, object]:
    return {
        "primitiveKey": event.primitive_key,
        "type": event.drift_type,
        "severity": event.severity,
        "reason": event.reason,
    }


def _error_payload(error: Exception) -> dict[str, object]:
    payload: dict[str, object] = {
        "error": getattr(error, "code", type(error).__name__),
        "message": str(error),
    }
    if isinstance(error, CatalogValidationError):
        payload["issues"] = list(error.issues)
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
