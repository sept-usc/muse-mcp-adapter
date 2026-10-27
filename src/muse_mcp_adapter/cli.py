import argparse
import getpass
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from .application import backup as backups
from .application.control_plane_service import ControlPlaneService
from .application.oauth_web import OAuthWebFlow, runtime_auth_factory
from .application.policy_template import draft_policies
from .application.secret_source import SecretSource
from .application.serve import (
    ServeConfig,
    endpoint_policy,
    load_serve_config,
    open_oauth_backend,
    open_repository,
)
from .catalog.serde import catalog_to_dict
from .connections import ConnectionCredentialResolver, ConnectionService
from .data_plane.tool_service import ToolDataPlaneService
from .discovery.client import ManifestDiscoveryClient
from .domain.errors import (
    CatalogValidationError,
    ConflictError,
    ControlPlaneError,
    DataPlaneError,
    EntityNotFoundError,
)
from .domain.models import (
    CatalogActivation,
    ConnectionRecord,
    DiscoverySnapshot,
    DriftEvent,
    ResolvedCredential,
    ServerRegistration,
)
from .persistence.owner_store import OwnerStore
from .persistence.sqlite_repository import SqliteControlPlaneRepository
from .transport.sdk_client import McpSdkClient, StreamableHttpSessionFactory
from .transport.security import EndpointSecurityPolicy

_DEFAULT_PUBLIC_URL = "http://127.0.0.1:8080"


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    try:
        config = _config(args)
        if args.command == "serve":
            return _serve(args, config)
        if args.command in ("backup", "restore"):
            print(json.dumps(_archive(args, config), indent=2, sort_keys=True))
            return 0
        with open_repository(args.database, config, _environment(config)) as repository:
            repository.initialize()
            result = _execute(args, repository, config)
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


def _config(args: argparse.Namespace) -> ServeConfig:
    path = args.config or os.environ.get("MUSE_ADAPTER_CONFIG")
    if path:
        return load_serve_config(Path(path))
    return ServeConfig(public_url=args.public_url or _DEFAULT_PUBLIC_URL)


def _archive(args: argparse.Namespace, config: ServeConfig) -> object:
    database = Path(args.database)
    if args.command == "restore":
        return backups.restore_backup(
            Path(args.source),
            database,
            artifact_root=config.artifact_root,
            force=args.force,
        )
    info = backups.create_backup(database, Path(args.to), artifact_root=config.artifact_root)
    removed = backups.prune(Path(args.to), args.keep) if args.keep else []
    return {
        "backup": str(info.path),
        "files": info.files,
        "bytes": info.bytes,
        "removedOld": [str(path) for path in removed],
    }


def _environment(config: ServeConfig) -> Mapping[str, str]:
    """The environment, plus secrets kept as files in the configured secrets directory."""
    configured = os.environ.get("MUSE_ADAPTER_SECRETS_DIR")
    directory = config.secrets_dir or (Path(configured) if configured else None)
    return SecretSource(os.environ, directory)


def _policy(args: argparse.Namespace, config: ServeConfig) -> EndpointSecurityPolicy:
    base = endpoint_policy(config)
    return EndpointSecurityPolicy(
        require_tls=base.require_tls and not args.allow_http,
        allow_private_networks=base.allow_private_networks or args.allow_private_network,
    )


def _serve(args: argparse.Namespace, config: ServeConfig) -> int:
    import uvicorn

    from .application.serve import build_application

    if not (args.config or os.environ.get("MUSE_ADAPTER_CONFIG")):
        raise ControlPlaneError("serve needs --config (or MUSE_ADAPTER_CONFIG)")
    environ = _environment(config)
    with open_repository(args.database, config, environ) as repository:
        repository.initialize()
        application = build_application(repository, config, environ, database=args.database)
        uvicorn.run(application, host=config.host, port=config.port, log_level="info")
    return 0


def _execute(
    args: argparse.Namespace, repository: SqliteControlPlaneRepository, config: ServeConfig
) -> object:
    policy = _policy(args, config)
    service = ControlPlaneService(repository, endpoint_policy=policy)
    actor = args.actor

    if args.command == "server-register":
        return _server_dict(
            service.register_server(
                server_id=args.server_id,
                display_name=args.display_name,
                endpoint=args.endpoint,
                auth_profile=args.auth_profile,
                provider_pack=args.provider_pack,
                actor=actor,
            )
        )

    if args.command == "server-list":
        return [_server_dict(server) for server in service.list_servers()]

    if args.command == "discover":
        manifest = _load_mapping(Path(args.manifest))
        snapshot = service.discover(args.server_id, ManifestDiscoveryClient(manifest), actor=actor)
        return _snapshot_dict(snapshot)

    if args.command == "discover-remote":
        client = McpSdkClient(
            StreamableHttpSessionFactory(
                endpoint_policy=policy,
                auth_factory=runtime_auth_factory(
                    repository, open_oauth_backend(args.database), public_url=config.public_url
                ),
            ),
            discovery_credential=_discovery_credential(args, repository, _environment(config)),
        )
        return _snapshot_dict(service.discover(args.server_id, client, actor=actor))

    if args.command in ("server-enable", "server-disable"):
        return _server_dict(
            service.set_server_enabled(
                args.server_id, enabled=args.command == "server-enable", actor=actor
            )
        )

    if args.command == "policy-template":
        snapshot = service.get_latest_discovery(args.server_id)
        return draft_policies(snapshot, expose_reads=args.expose_reads)

    if args.command == "publish":
        catalog = service.publish_catalog(
            server_id=args.server_id,
            catalog_version=args.catalog_version,
            policies=_load_policy_list(Path(args.policies)),
            actor=actor,
        )
        return catalog_to_dict(catalog)

    if args.command == "activate":
        return _activation_dict(
            service.activate_catalog(args.server_id, args.catalog_version, actor=actor)
        )

    if args.command == "rollback":
        return _activation_dict(service.rollback_catalog(args.server_id, actor=actor))

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
            result["latestDiscovery"] = _snapshot_dict(service.get_latest_discovery(args.server_id))
        except EntityNotFoundError:
            result["latestDiscovery"] = None
        try:
            result["activeCatalog"] = catalog_to_dict(service.get_active_catalog(args.server_id))
        except EntityNotFoundError:
            result["activeCatalog"] = None
        result["connections"] = [
            _connection_dict(item, config.public_url)
            for item in repository.list_connections(args.server_id)
        ]
        return result

    connections = ConnectionService(repository)

    if args.command == "link":
        scopes = [item for item in (args.scopes or "").split(",") if item.strip()]
        linked = connections.link(
            server_id=args.server_id,
            label=args.label,
            credential_kind=args.credential,
            credential_ref=args.token_env or "",
            scopes=[item.strip() for item in scopes],
            actor=actor,
        )
        record = linked.record
        connector_url = _connector_url(config.public_url, record)
        result = _connection_dict(record, config.public_url)
        result["token"] = linked.token
        result["connectionCode"] = linked.link_code
        result["connectionCodeExpiresAt"] = linked.link_code_expires_at
        result["secretsNote"] = (
            "The token and connection code are shown once. Give them only to Muse."
        )
        result["museInstructions"] = _muse_instructions(
            record.label, connector_url, linked.token, linked.link_code
        )
        if record.credential_kind == "oauth":
            result["loginUrl"] = _flow(repository, config, args).login_link(record.connection_id)
            result["loginNote"] = "Open this one-time link in your browser to sign in."
        return result

    if args.command == "links":
        return [
            _connection_dict(item, config.public_url)
            for item in connections.list(getattr(args, "server_id", None))
        ]

    if args.command == "unlink":
        return _connection_dict(
            connections.unlink(args.connection_id, actor=actor), config.public_url
        )

    if args.command == "rotate-token":
        rotated = connections.rotate_token(args.connection_id, actor=actor)
        result = _connection_dict(rotated.record, config.public_url)
        result["token"] = rotated.token
        result["connectionCode"] = rotated.link_code
        result["connectionCodeExpiresAt"] = rotated.link_code_expires_at
        result["museInstructions"] = _muse_instructions(
            rotated.record.label,
            _connector_url(config.public_url, rotated.record),
            rotated.token,
            rotated.link_code,
        )
        return result

    if args.command == "link-code":
        code, expires = connections.issue_link_code(args.connection_id, actor=actor)
        return {
            "connectionId": args.connection_id,
            "connectionCode": code,
            "connectionCodeExpiresAt": expires,
            "note": "Enter this code on the page Muse opens when you sign in to the connector.",
        }

    if args.command == "login":
        record = repository.get_connection(args.connection_id)
        if record.credential_kind != "oauth":
            raise ControlPlaneError("Only OAuth connections need to sign in")
        return {
            "connectionId": record.connection_id,
            "loginUrl": _flow(repository, config, args).login_link(record.connection_id),
            "note": "Open this one-time link in your browser. It expires in 15 minutes.",
        }

    if args.command == "reconcile":
        return _reconcile(args, repository)

    if args.command in ("owner-passcode", "approvals", "approval-decide"):
        return _owner(args, repository)

    raise AssertionError(f"Unhandled command {args.command}")


def _flow(
    repository: SqliteControlPlaneRepository, config: ServeConfig, args: argparse.Namespace
) -> OAuthWebFlow:
    return OAuthWebFlow(
        repository=repository,
        public_url=config.public_url,
        backend=open_oauth_backend(args.database),
        endpoint_policy=_policy(args, config),
        clock=ConnectionService(repository)._clock,  # pyright: ignore[reportPrivateUsage]
    )


def _discovery_credential(
    args: argparse.Namespace, repository: SqliteControlPlaneRepository, environ: Mapping[str, str]
) -> ResolvedCredential | None:
    if args.connection_id:
        connection = repository.get_connection(args.connection_id)
        return ConnectionCredentialResolver(repository, environ).resolve(
            local_identity_id=connection.identity_id,
            connection_id=connection.connection_id,
            server_id=args.server_id,
            required_scopes=frozenset(),
        )
    if args.token_env:
        token = environ.get(args.token_env)
        if not token:
            raise ControlPlaneError(f"Environment variable {args.token_env} is not set")
        return ResolvedCredential(
            credential_ref=f"environment://{args.token_env}",
            access_token=token,
            local_identity_id="administrative-discovery",
            connection_id="administrative-discovery",
            server_id=args.server_id,
        )
    return None


def _reconcile(args: argparse.Namespace, repository: SqliteControlPlaneRepository) -> object:
    """Settle an UNKNOWN write after the owner has checked the downstream system."""
    from .credentials.resolver import InMemoryCredentialResolver
    from .data_plane.serde import response_from_dict

    class _NoDownstream:
        def call_tool(self, **_: object) -> object:
            raise ControlPlaneError("Reconciliation never calls the downstream server")

    service = ToolDataPlaneService(
        repository,
        InMemoryCredentialResolver(),
        _NoDownstream(),  # type: ignore[arg-type]
        clock=ConnectionService(repository)._clock,  # pyright: ignore[reportPrivateUsage]
    )
    response = None
    if args.outcome == "completed":
        if not args.response:
            raise ControlPlaneError("--response (a JSON file) is required for 'completed'")
        response = response_from_dict(_load_mapping(Path(args.response)))
    record = service.reconcile_unknown(
        args.operation_id, actor=args.actor, outcome=args.outcome, response=response
    )
    return {"operationId": record.operation_id, "status": record.status}


def _owner(args: argparse.Namespace, repository: SqliteControlPlaneRepository) -> object:
    """The owner's side of approvals, from the machine the adapter runs on."""
    store = OwnerStore(repository)
    now = ConnectionService(repository)._clock()  # pyright: ignore[reportPrivateUsage]
    if args.command == "owner-passcode":
        name = args.passcode_env
        passcode = os.environ.get(name, "") if name else getpass.getpass("New approval passcode: ")
        if not name and getpass.getpass("Repeat it: ") != passcode:
            raise ControlPlaneError("The two passcodes did not match")
        try:
            store.set_passcode(passcode, now)
        except ValueError as error:
            raise ControlPlaneError(str(error)) from error
        return {
            "passcode": "set",
            "note": "Enter it only on the approval page. Never tell the assistant.",
        }
    if args.command == "approvals":
        return [
            {
                "requestId": item.request_id,
                "status": item.status,
                "operation": item.public_operation_id,
                "reasons": list(item.reasons),
                "arguments": item.arguments,
                "createdAt": item.created_at,
                "expiresAt": item.expires_at,
            }
            for item in store.list_requests(None if args.all else "pending")
        ]
    try:
        decided = store.decide(args.request_id, approve=args.approve, by=args.actor, now=now)
    except ConflictError as error:
        raise ControlPlaneError(str(error)) from error
    if args.approve and args.remember and decided.recipients:
        store.remember_recipients(decided.connection_id, decided.recipients, now)
    return {"requestId": decided.request_id, "status": decided.status, "by": args.actor}


def _connector_url(public_url: str, record: ConnectionRecord) -> str:
    return f"{public_url.rstrip('/')}/c/{record.slug}/openapi.json"


def _muse_instructions(
    label: str, connector_url: str, token: str, connection_code: str | None
) -> str:
    sign_in = (
        f"If Muse offers to sign in, choose that: enter the connection code {connection_code} "
        "on the page that opens. "
        if connection_code
        else ""
    )
    return (
        f'Tell Muse: "Create a connector called {label} from the API spec at {connector_url}." '
        f"{sign_in}If Muse asks for a bearer token instead, give it: {token}"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="muse-mcp-adapter")
    parser.add_argument("--database", default="adapter.db")
    parser.add_argument(
        "--config", help="Service configuration (also read from MUSE_ADAPTER_CONFIG)"
    )
    parser.add_argument("--public-url", help="Public origin of this service when no --config")
    parser.add_argument(
        "--actor",
        default=os.environ.get("USER", "local-administrator"),
        help="Name recorded in the administrative audit trail",
    )
    parser.add_argument(
        "--allow-http",
        action="store_true",
        help="Development only: allow plain-HTTP MCP servers",
    )
    parser.add_argument(
        "--allow-private-network",
        action="store_true",
        help="Development only: allow MCP servers on loopback or private addresses",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def server_command(name: str, help_text: str | None = None) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--server-id", required=True)
        return command

    register = commands.add_parser("server-register", help="Register an MCP server")
    register.add_argument("--server-id", required=True)
    register.add_argument("--display-name", required=True)
    register.add_argument("--endpoint", required=True)
    register.add_argument("--auth-profile", required=True)
    register.add_argument("--provider-pack")

    commands.add_parser("server-list", help="List registered MCP servers")
    commands.add_parser("serve", help="Run the Muse-facing HTTP service")

    backup = commands.add_parser(
        "backup", help="Copy the database, key file, token store and artifacts to a folder"
    )
    backup.add_argument("--to", required=True, help="Folder that holds the backups")
    backup.add_argument("--keep", type=int, default=0, help="Keep only the newest N backups")

    restore = commands.add_parser(
        "restore", help="Put a backup back (stop the service first; existing data is moved aside)"
    )
    restore.add_argument("--from", dest="source", required=True, help="A backup folder")
    restore.add_argument("--force", action="store_true", help="Replace existing data")

    discover = server_command("discover", "Record primitives from a manifest file")
    discover.add_argument("--manifest", required=True)

    remote = server_command("discover-remote", "Connect to the MCP server and record primitives")
    remote.add_argument("--token-env", help="Environment variable holding a bearer token")
    remote.add_argument("--connection-id", help="Use this connection's credential")

    server_command("server-enable", "Resume traffic for a server")
    server_command("server-disable", "Stop all traffic for a server")

    template = server_command("policy-template", "Draft an exposure policy to review and edit")
    template.add_argument(
        "--expose-reads",
        action="store_true",
        help="Draft read-only primitives as allowed (writes always start disallowed)",
    )

    publish = server_command("publish", "Compile a catalog from discovery and a policy file")
    publish.add_argument("--catalog-version", required=True)
    publish.add_argument("--policies", required=True)

    activate = server_command("activate", "Make a catalog version live")
    activate.add_argument("--catalog-version", required=True)

    server_command("rollback", "Return to the previously active catalog")
    server_command("drift", "Compare the live server with the active catalog")
    server_command("status", "Show a server, its catalog and its connections")

    link = server_command("link", "Create a connection Muse will use to reach a server")
    link.add_argument("--label", required=True, help="A name for this Muse connection")
    link.add_argument(
        "--credential",
        choices=("none", "env_token", "oauth"),
        default="none",
        help="How the adapter authenticates to the MCP server",
    )
    link.add_argument("--token-env", help="Environment variable holding the server token")
    link.add_argument("--scopes", help="Comma-separated scopes to request or assume")

    links = commands.add_parser("links", help="List connections")
    links.add_argument("--server-id")

    unlink = commands.add_parser("unlink", help="Revoke a connection")
    unlink.add_argument("--connection-id", required=True)

    rotate = commands.add_parser(
        "rotate-token", help="Issue a new token and code, ending every session for a connection"
    )
    rotate.add_argument("--connection-id", required=True)

    code = commands.add_parser(
        "link-code", help="Issue a new connection code for signing in from Muse"
    )
    code.add_argument("--connection-id", required=True)

    login = commands.add_parser("login", help="Get a sign-in link for an OAuth connection")
    login.add_argument("--connection-id", required=True)

    reconcile = commands.add_parser(
        "reconcile", help="Settle a write whose outcome is unknown after checking the system"
    )
    reconcile.add_argument("--operation-id", required=True)
    reconcile.add_argument("--outcome", choices=("not_applied", "completed"), required=True)
    reconcile.add_argument("--response", help="JSON file with the result (for 'completed')")

    passcode = commands.add_parser(
        "owner-passcode",
        help="Set the passcode that approves held actions (asked for, never shown)",
    )
    passcode.add_argument(
        "--passcode-env", help="Read the passcode from this environment variable (for automation)"
    )

    approvals = commands.add_parser("approvals", help="List actions waiting for the owner")
    approvals.add_argument("--all", action="store_true", help="Include decided and used ones")

    decide = commands.add_parser(
        "approval-decide", help="Approve or decline a held action from this machine"
    )
    decide.add_argument("--request-id", required=True)
    choice = decide.add_mutually_exclusive_group(required=True)
    choice.add_argument("--approve", action="store_true")
    choice.add_argument("--decline", action="store_true")
    decide.add_argument(
        "--remember",
        action="store_true",
        help="With --approve: do not ask again for its recipients",
    )
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


def _connection_dict(record: ConnectionRecord, public_url: str) -> dict[str, object]:
    return {
        "connectionId": record.connection_id,
        "serverId": record.server_id,
        "label": record.label,
        "credential": record.credential_kind,
        "scopes": list(record.scopes),
        "connectorUrl": _connector_url(public_url, record),
        "revoked": record.revoked_at is not None,
        "createdAt": record.created_at,
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
