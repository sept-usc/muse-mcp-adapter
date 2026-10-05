from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import cast
from uuid import uuid4

from ..data_plane.read_service import ReadDataPlaneService
from ..data_plane.tool_service import ToolDataPlaneService
from ..domain.errors import AuthenticationError, AuthorizationError, RequestValidationError
from ..domain.models import ApprovalEvidence, DataPlaneRequest, DataPlaneResponse, OperationRecord
from ..operations.artifacts import FileArtifactStore
from ..operations.lifecycle import OperationLifecycleService
from ..operations.runtime_controls import (
    AdapterMetrics,
    FixedWindowRateLimiter,
    RequestConcurrencyLimiter,
)
from ..persistence.sqlite_repository import SqliteControlPlaneRepository
from .openapi import compile_openapi


@dataclass(frozen=True, slots=True)
class Principal:
    local_identity_id: str
    connection_ids: frozenset[str]
    server_ids: frozenset[str]
    administrator: bool = False


class BearerAuthenticator:
    """Constant-time bearer authentication for customer-managed ingress tokens."""

    def __init__(self, bindings: Mapping[str, Principal]) -> None:
        self._bindings = tuple(
            (hashlib.sha256(token.encode()).digest(), principal)
            for token, principal in bindings.items()
        )

    def authenticate(self, authorization: str | None) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthenticationError("A bearer token is required")
        presented = hashlib.sha256(authorization[7:].encode()).digest()
        matched: Principal | None = None
        for expected, principal in self._bindings:
            if hmac.compare_digest(presented, expected):
                matched = principal
        if matched is None:
            raise AuthenticationError("Bearer token is invalid")
        return matched


class AdapterRuntime:
    """Mandatory production request path joining authz, controls, execution, and audit."""

    def __init__(
        self,
        *,
        repository: SqliteControlPlaneRepository,
        read_service: ReadDataPlaneService,
        tool_service: ToolDataPlaneService,
        lifecycle: OperationLifecycleService,
        rate_limiter: FixedWindowRateLimiter,
        concurrency_limiter: RequestConcurrencyLimiter,
        metrics: AdapterMetrics,
        clock: Callable[[], str],
        monotonic: Callable[[], float] = time.monotonic,
        artifact_store: FileArtifactStore | None = None,
    ) -> None:
        self._repository = repository
        self._reads = read_service
        self._tools = tool_service
        self._lifecycle = lifecycle
        self._rate = rate_limiter
        self._concurrency = concurrency_limiter
        self._metrics = metrics
        self._clock = clock
        self._monotonic = monotonic
        self._artifacts = artifact_store

    @property
    def metrics(self) -> AdapterMetrics:
        return self._metrics

    def execute(
        self,
        principal: Principal,
        public_operation_id: str,
        payload: Mapping[str, object],
    ) -> DataPlaneResponse:
        server_id = _required_string(payload, "serverId")
        connection_id = _required_string(payload, "connectionId")
        self._authorize_binding(principal, server_id, connection_id)
        arguments = payload.get("arguments")
        if not isinstance(arguments, Mapping):
            raise RequestValidationError("Request arguments must be an object")
        request = DataPlaneRequest(
            request_id=_optional_string(payload, "requestId") or str(uuid4()),
            server_id=server_id,
            public_operation_id=public_operation_id,
            local_identity_id=principal.local_identity_id,
            connection_id=connection_id,
            arguments=cast(Mapping[str, object], arguments),
            idempotency_key=_optional_string(payload, "idempotencyKey"),
            approval=_approval(payload.get("approval")),
        )
        catalog = self._repository.get_active_catalog(server_id)
        primitive = next(
            (
                item
                for item in catalog.primitives
                if item.public_operation_id == public_operation_id
            ),
            None,
        )
        if primitive is None:
            raise AuthorizationError("The requested operation is not published")
        key = f"{principal.local_identity_id}:{server_id}:{public_operation_id}"
        self._rate.acquire(key)
        started = self._monotonic()
        response: DataPlaneResponse | None = None
        error: BaseException | None = None
        try:
            with self._concurrency.slot(server_id):
                response = (
                    self._tools.execute(request)
                    if primitive.primitive_type == "tool"
                    else self._reads.execute(request)
                )
                self._metrics.increment("requests.completed")
                return response
        except BaseException as raised:
            error = raised
            self._metrics.increment("requests.failed")
            raise
        finally:
            self._repository.record_runtime_audit(
                occurred_at=self._clock(),
                request_id=request.request_id,
                server_id=server_id,
                public_operation_id=public_operation_id,
                local_identity_id=principal.local_identity_id,
                connection_id=connection_id,
                catalog_version=catalog.catalog_version,
                classification=primitive.classification,
                approval_id=request.approval.approval_id if request.approval else None,
                outcome="completed" if response is not None else "failed",
                error_code=getattr(error, "code", type(error).__name__) if error else None,
                duration_ms=max(0, int((self._monotonic() - started) * 1000)),
            )

    def cancel(
        self, principal: Principal, operation_id: str, *, reason: str
    ) -> OperationRecord:
        operation = self._owned_operation(principal, operation_id)
        return self._lifecycle.cancel(operation.operation_id, reason=reason)

    def resume(
        self,
        principal: Principal,
        operation_id: str,
        payload: Mapping[str, object],
    ) -> DataPlaneResponse:
        operation = self._owned_operation(principal, operation_id)
        server_id = _required_string(payload, "serverId")
        connection_id = _required_string(payload, "connectionId")
        if server_id != operation.server_id or connection_id != operation.connection_id:
            raise AuthorizationError("Resume request does not match the operation binding")
        arguments = payload.get("arguments")
        inputs = payload.get("inputs")
        if not isinstance(arguments, Mapping) or not isinstance(inputs, Mapping):
            raise RequestValidationError("Resume arguments and inputs must be objects")
        request = DataPlaneRequest(
            request_id=operation.request_id,
            server_id=server_id,
            public_operation_id=operation.public_operation_id,
            local_identity_id=principal.local_identity_id,
            connection_id=connection_id,
            arguments=cast(Mapping[str, object], arguments),
            idempotency_key=operation.idempotency_key,
            approval=_approval(payload.get("approval")),
        )
        return self._tools.resume_with_input(
            request,
            operation_id=operation_id,
            inputs=cast(Mapping[str, object], inputs),
        )

    def operation(self, principal: Principal, operation_id: str) -> OperationRecord:
        return self._owned_operation(principal, operation_id)

    def recover_stale(self, *, updated_before: str) -> tuple[OperationRecord, ...]:
        return self._lifecycle.recover_stale(updated_before=updated_before)

    def openapi(self, principal: Principal, server_id: str) -> Mapping[str, object]:
        if server_id not in principal.server_ids:
            raise AuthorizationError("The caller is not authorized for this server")
        return compile_openapi(self._repository.get_active_catalog(server_id))

    def artifact(self, principal: Principal, artifact_id: str) -> tuple[bytes, str]:
        if self._artifacts is None:
            raise AuthorizationError("Artifact storage is not configured")
        record = self._artifacts.metadata(artifact_id)
        self._owned_operation(principal, record.operation_id)
        return (
            self._artifacts.read(artifact_id, operation_id=record.operation_id),
            record.media_type,
        )

    def _owned_operation(self, principal: Principal, operation_id: str) -> OperationRecord:
        operation = self._repository.get_operation(operation_id)
        self._authorize_binding(principal, operation.server_id, operation.connection_id)
        owner = operation.local_identity_id == principal.local_identity_id
        if not owner and not principal.administrator:
            raise AuthorizationError("The operation does not belong to this identity")
        return operation

    @staticmethod
    def _authorize_binding(
        principal: Principal, server_id: str, connection_id: str
    ) -> None:
        if server_id not in principal.server_ids or connection_id not in principal.connection_ids:
            raise AuthorizationError("The caller is not authorized for this server connection")


def _approval(value: object) -> ApprovalEvidence | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise RequestValidationError("Approval evidence must be an object")
    item = cast(Mapping[str, object], value)
    return ApprovalEvidence(
        approval_id=_required_string(item, "approvalId"),
        classification=_required_string(item, "classification"),
        action_digest=_required_string(item, "actionDigest"),
        approved_at=_required_string(item, "approvedAt"),
        expires_at=_required_string(item, "expiresAt"),
        signature=_required_string(item, "signature"),
    )


def _required_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise RequestValidationError(f"{key} is required")
    return item


def _optional_string(value: Mapping[str, object], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item:
        raise RequestValidationError(f"{key} must be a non-empty string")
    return item
