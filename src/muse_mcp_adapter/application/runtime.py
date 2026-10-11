from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import uuid4

from ..catalog.canonical import canonical_json
from ..connections import ConnectionService
from ..data_plane.read_service import ReadDataPlaneService
from ..data_plane.tool_service import ToolDataPlaneService
from ..domain.errors import (
    AuthenticationError,
    AuthorizationError,
    EntityNotFoundError,
    ReauthorizationRequiredError,
    RequestValidationError,
)
from ..domain.models import (
    DataPlaneRequest,
    DataPlaneResponse,
    OperationEvent,
    OperationRecord,
)
from ..operations.artifacts import FileArtifactStore
from ..operations.lifecycle import OperationLifecycleService
from ..operations.runtime_controls import (
    AdapterMetrics,
    FixedWindowRateLimiter,
    RequestConcurrencyLimiter,
)
from ..persistence.sqlite_repository import SqliteControlPlaneRepository
from .openapi import compile_openapi

MAX_EVENTS_SHOWN = 400
_IDEMPOTENCY_KEY = re.compile(r"^[\x21-\x7e]{1,200}$")


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller: one connection, and through it one owner and one server."""

    connection_id: str
    identity_id: str
    server_id: str
    slug: str

    @property
    def local_identity_id(self) -> str:
        return self.identity_id


class AdapterRuntime:
    """Request path joining authentication, controls, execution and audit."""

    def __init__(
        self,
        *,
        repository: SqliteControlPlaneRepository,
        connections: ConnectionService,
        read_service: ReadDataPlaneService,
        tool_service: ToolDataPlaneService,
        lifecycle: OperationLifecycleService,
        rate_limiter: FixedWindowRateLimiter,
        concurrency_limiter: RequestConcurrencyLimiter,
        metrics: AdapterMetrics,
        clock: Callable[[], str],
        public_url: str,
        monotonic: Callable[[], float] = time.monotonic,
        artifact_store: FileArtifactStore | None = None,
        idempotency_window_seconds: int = 30,
        reauthorization_link: Callable[[str], str] | None = None,
    ) -> None:
        self._repository = repository
        self._connections = connections
        self._reads = read_service
        self._tools = tool_service
        self._lifecycle = lifecycle
        self._rate = rate_limiter
        self._concurrency = concurrency_limiter
        self._metrics = metrics
        self._clock = clock
        self._public_url = public_url
        self._monotonic = monotonic
        self._artifacts = artifact_store
        self._window = idempotency_window_seconds
        self._reauthorization_link = reauthorization_link

    @property
    def public_url(self) -> str:
        return self._public_url

    @property
    def metrics(self) -> AdapterMetrics:
        return self._metrics

    def authenticate(self, authorization: str | None, slug: str) -> Principal:
        """Resolve the bearer token, and require that it belongs to the connector in the URL."""
        token = None
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        connection = self._connections.authenticate(token)
        if connection.slug != slug:
            # A valid token for another connector must not reveal that this one exists.
            raise AuthenticationError("Bearer token is invalid")
        return Principal(
            connection_id=connection.connection_id,
            identity_id=connection.identity_id,
            server_id=connection.server_id,
            slug=connection.slug,
        )

    def execute(
        self,
        principal: Principal,
        public_operation_id: str,
        arguments: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> DataPlaneResponse:
        server = self._repository.get_server(principal.server_id)
        if not server.enabled:
            raise AuthorizationError("This connector is disabled")
        catalog = self._repository.get_active_catalog(principal.server_id)
        primitive = next(
            (
                item
                for item in catalog.primitives
                if item.public_operation_id == public_operation_id
            ),
            None,
        )
        if primitive is None:
            raise EntityNotFoundError(f"Operation {public_operation_id} is not available")
        if idempotency_key is not None and not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise RequestValidationError("Idempotency-Key must be 1 to 200 printable characters")
        if primitive.classification != "read" and idempotency_key is None:
            idempotency_key = self._derive_key(principal, public_operation_id, arguments)

        request = DataPlaneRequest(
            request_id=request_id or str(uuid4()),
            server_id=principal.server_id,
            public_operation_id=public_operation_id,
            local_identity_id=principal.identity_id,
            connection_id=principal.connection_id,
            arguments=arguments,
            idempotency_key=idempotency_key,
        )
        self._rate.acquire(principal.connection_id)
        started = self._monotonic()
        response: DataPlaneResponse | None = None
        error: BaseException | None = None
        try:
            with self._concurrency.slot(principal.server_id):
                response = (
                    self._tools.execute(request)
                    if primitive.primitive_type == "tool"
                    else self._reads.execute(request)
                )
                self._metrics.increment("requests.completed")
                return response
        except ReauthorizationRequiredError as raised:
            error = raised
            self._metrics.increment("requests.failed")
            raise self._with_link(principal, raised) from None
        except BaseException as raised:
            error = raised
            self._metrics.increment("requests.failed")
            raise
        finally:
            self._repository.record_runtime_audit(
                occurred_at=self._clock(),
                request_id=request.request_id,
                server_id=principal.server_id,
                public_operation_id=public_operation_id,
                local_identity_id=principal.identity_id,
                connection_id=principal.connection_id,
                catalog_version=catalog.catalog_version,
                classification=primitive.classification,
                approval_id=None,
                outcome="completed" if response is not None else "failed",
                error_code=getattr(error, "code", type(error).__name__) if error else None,
                duration_ms=max(0, int((self._monotonic() - started) * 1000)),
            )

    def resume(
        self,
        principal: Principal,
        operation_id: str,
        arguments: Mapping[str, object],
        inputs: Mapping[str, object],
    ) -> DataPlaneResponse:
        operation = self._owned_operation(principal, operation_id)
        request = DataPlaneRequest(
            request_id=operation.request_id,
            server_id=principal.server_id,
            public_operation_id=operation.public_operation_id,
            local_identity_id=principal.identity_id,
            connection_id=principal.connection_id,
            arguments=arguments,
            idempotency_key=operation.idempotency_key,
        )
        try:
            return self._tools.resume_with_input(request, operation_id=operation_id, inputs=inputs)
        except ReauthorizationRequiredError as raised:
            raise self._with_link(principal, raised) from None

    def operation(self, principal: Principal, operation_id: str) -> OperationRecord:
        return self._owned_operation(principal, operation_id)

    def events(self, principal: Principal, operation_id: str) -> tuple[OperationEvent, ...]:
        """Progress, log and lifecycle events of one of the caller's own operations."""
        self._owned_operation(principal, operation_id)
        return self._repository.list_operation_events(operation_id)[:MAX_EVENTS_SHOWN]

    def cancel(self, principal: Principal, operation_id: str, *, reason: str) -> OperationRecord:
        operation = self._owned_operation(principal, operation_id)
        return self._lifecycle.cancel(operation.operation_id, reason=reason)

    def recover_stale(self, *, updated_before: str) -> tuple[OperationRecord, ...]:
        return self._lifecycle.recover_stale(updated_before=updated_before)

    def openapi(self, slug: str) -> Mapping[str, object]:
        """The public connector description Muse reads. It names tools but holds no secrets."""
        connection = self._connections.by_slug(slug)
        if not self._repository.get_server(connection.server_id).enabled:
            raise EntityNotFoundError("Connector was not found")
        catalog = self._repository.get_active_catalog(connection.server_id)
        return compile_openapi(catalog, public_url=self._public_url, slug=slug)

    def artifact(self, principal: Principal, artifact_id: str) -> tuple[bytes, str]:
        if self._artifacts is None:
            raise EntityNotFoundError("Artifact storage is not configured")
        record = self._artifacts.metadata(artifact_id)
        self._owned_operation(principal, record.operation_id)
        return (
            self._artifacts.read(artifact_id, operation_id=record.operation_id),
            record.media_type,
        )

    def _owned_operation(self, principal: Principal, operation_id: str) -> OperationRecord:
        operation = self._repository.get_operation(operation_id)
        if operation.connection_id != principal.connection_id:
            # Another connection's operation is indistinguishable from a missing one.
            raise EntityNotFoundError(f"Operation {operation_id} was not found")
        return operation

    def _derive_key(
        self, principal: Principal, operation_id: str, arguments: Mapping[str, object]
    ) -> str:
        """Pick the idempotency key for a write when Muse sends none.

        An identical request is treated as a retry only while it could still be one: the first is
        still running, its outcome is unknown, or it finished a moment ago (so the caller may
        simply not have received the answer). After that window an identical request is a new,
        deliberate action and runs again; the same applies after a failure, which is safe to
        retry. Keys the caller supplies are never touched: they are honoured for as long as the
        operation is stored.
        """
        digest = hashlib.sha256(
            canonical_json(
                {
                    "connection": principal.connection_id,
                    "operation": operation_id,
                    "arguments": arguments,
                }
            ).encode()
        ).hexdigest()
        now = datetime.fromisoformat(self._clock().replace("Z", "+00:00"))
        for generation in range(10_000):
            key = f"auto-{digest[:36]}-{generation}"
            earlier = self._repository.find_operation_by_idempotency(
                server_id=principal.server_id,
                public_operation_id=operation_id,
                local_identity_id=principal.identity_id,
                connection_id=principal.connection_id,
                idempotency_key=key,
            )
            if earlier is None or earlier.status not in ("COMPLETED", "FAILED"):
                return key  # new, or still in flight / of unknown outcome: stay on this one
            if earlier.status == "COMPLETED":
                finished = datetime.fromisoformat(earlier.updated_at.replace("Z", "+00:00"))
                if (now - finished).total_seconds() <= self._window:
                    return key  # a retry of something that just finished: replay its result
        raise RequestValidationError("Too many identical requests; send an Idempotency-Key")

    def _with_link(
        self, principal: Principal, error: ReauthorizationRequiredError
    ) -> ReauthorizationRequiredError:
        link = (
            self._reauthorization_link(principal.connection_id)
            if self._reauthorization_link is not None
            else None
        )
        message = str(error)
        if link:
            message += f" Open this link in your browser to reconnect: {link}"
        return ReauthorizationRequiredError(message, authorization_url=link)


def payload_object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise RequestValidationError(f"{label} must be a JSON object")
    return cast(Mapping[str, object], value)
