import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from urllib.parse import urlsplit

from ..control_plane.compile_catalog import compile_catalog
from ..discovery.client import DiscoveryClient
from ..domain.errors import InvalidStateError
from ..domain.models import (
    CatalogActivation,
    DiscoverySnapshot,
    DriftEvent,
    PublishedCatalog,
    ServerRegistration,
)
from ..drift.detect_drift import detect_drift
from ..persistence.sqlite_repository import SqliteControlPlaneRepository
from ..providers import load_provider_pack, validate_provider_registration
from ..transport.security import EndpointSecurityPolicy

_SERVER_ID = re.compile(r"^[a-z][a-z0-9-]{2,63}$")


class ControlPlaneService:
    def __init__(
        self,
        repository: SqliteControlPlaneRepository,
        clock: Callable[[], str] | None = None,
        endpoint_policy: EndpointSecurityPolicy | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or _utc_now
        self._endpoint_policy = endpoint_policy or EndpointSecurityPolicy()

    def register_server(
        self,
        *,
        server_id: str,
        display_name: str,
        endpoint: str,
        auth_profile: str,
        provider_pack: str | None = None,
        actor: str = "local-administrator",
    ) -> ServerRegistration:
        issues: list[str] = []
        if not _SERVER_ID.fullmatch(server_id):
            issues.append("server_id must match ^[a-z][a-z0-9-]{2,63}$")
        if not display_name.strip():
            issues.append("display_name is required")
        if not _http_url(endpoint):
            issues.append("endpoint must be an HTTP or HTTPS URL")
        else:
            try:
                self._endpoint_policy.validate(endpoint, resolve=False)
            except Exception as error:
                issues.append(str(error))
        if not auth_profile.strip():
            issues.append("auth_profile is required")
        if provider_pack is not None:
            try:
                validate_provider_registration(
                    load_provider_pack(provider_pack),
                    endpoint=endpoint,
                    auth_mode=auth_profile,
                )
            except Exception as error:
                issues.append(str(error))
        if issues:
            raise InvalidStateError("; ".join(issues))

        now = self._clock()
        registered = self._repository.create_server(
            ServerRegistration(
                server_id=server_id,
                display_name=display_name,
                endpoint=endpoint,
                auth_profile=auth_profile,
                provider_pack=provider_pack,
                enabled=True,
                created_at=now,
                updated_at=now,
            )
        )
        self._audit(
            actor,
            "server.register",
            server_id,
            {"providerPack": provider_pack},
            occurred_at=now,
        )
        return registered

    def list_servers(self) -> tuple[ServerRegistration, ...]:
        return self._repository.list_servers()

    def discover(
        self,
        server_id: str,
        client: DiscoveryClient,
        *,
        actor: str = "local-administrator",
    ) -> DiscoverySnapshot:
        server = self._repository.get_server(server_id)
        if not server.enabled:
            raise InvalidStateError(f"Server {server_id} is disabled")
        snapshot = client.discover(server)
        if snapshot.server_id != server_id:
            raise InvalidStateError("Discovery client returned a snapshot for another server")
        saved = self._repository.save_discovery(snapshot)
        self._audit(
            actor,
            "server.discover",
            server_id,
            {"snapshot": saved.snapshot_digest},
            occurred_at=saved.discovered_at,
        )
        return saved

    def publish_catalog(
        self,
        *,
        server_id: str,
        catalog_version: str,
        policies: Sequence[Mapping[str, object]],
        actor: str = "local-administrator",
    ) -> PublishedCatalog:
        server = self._repository.get_server(server_id)
        if not server.enabled:
            raise InvalidStateError(f"Server {server_id} is disabled")
        snapshot = self._repository.get_latest_discovery(server_id)
        server_definition: dict[str, object] = {
            "id": server.server_id,
            "endpoint": server.endpoint,
            "authProfile": server.auth_profile,
        }
        if server.provider_pack is not None:
            server_definition["providerPack"] = server.provider_pack

        published_at = self._clock()
        catalog = compile_catalog(
            catalog_version=catalog_version,
            server=server_definition,
            protocol_version=snapshot.protocol_version,
            discovered_at=snapshot.discovered_at,
            published_at=published_at,
            primitives=snapshot.primitives,
            policies=policies,
        )
        saved = self._repository.save_catalog(catalog)
        self._audit(
            actor,
            "catalog.publish",
            server_id,
            {"version": catalog_version},
            occurred_at=published_at,
        )
        return saved

    def activate_catalog(
        self,
        server_id: str,
        catalog_version: str,
        *,
        actor: str = "local-administrator",
    ) -> CatalogActivation:
        activated_at = self._clock()
        activation = self._repository.activate_catalog(server_id, catalog_version, activated_at)
        self._audit(
            actor,
            "catalog.activate",
            server_id,
            {"version": catalog_version},
            occurred_at=activated_at,
        )
        return activation

    def rollback_catalog(
        self, server_id: str, *, actor: str = "local-administrator"
    ) -> CatalogActivation:
        activated_at = self._clock()
        activation = self._repository.rollback_catalog(server_id, activated_at)
        self._audit(
            actor,
            "catalog.rollback",
            server_id,
            {"version": activation.catalog_version},
            occurred_at=activated_at,
        )
        return activation

    def set_server_enabled(
        self,
        server_id: str,
        *,
        enabled: bool,
        actor: str = "local-administrator",
    ) -> ServerRegistration:
        updated_at = self._clock()
        server = self._repository.set_server_enabled(server_id, enabled, updated_at)
        self._audit(
            actor,
            "server.enable" if enabled else "server.disable",
            server_id,
            {},
            occurred_at=updated_at,
        )
        return server

    def get_active_catalog(self, server_id: str) -> PublishedCatalog:
        return self._repository.get_active_catalog(server_id)

    def get_latest_discovery(self, server_id: str) -> DiscoverySnapshot:
        return self._repository.get_latest_discovery(server_id)

    def get_drift(self, server_id: str) -> tuple[DriftEvent, ...]:
        active = self._repository.get_active_catalog(server_id)
        latest = self._repository.get_latest_discovery(server_id)
        return detect_drift(active, latest.primitives)

    def _audit(
        self,
        actor: str,
        action: str,
        server_id: str | None,
        data: Mapping[str, object],
        *,
        occurred_at: str,
    ) -> None:
        if not actor:
            raise InvalidStateError("Administrative actor is required")
        self._repository.record_admin_audit(
            occurred_at=occurred_at,
            actor=actor,
            action=action,
            server_id=server_id,
            data=data,
        )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _http_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)
