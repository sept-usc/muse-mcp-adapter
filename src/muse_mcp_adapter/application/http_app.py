from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import cast

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..data_plane.serde import response_to_dict
from ..domain.errors import (
    ApprovalRequiredError,
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    ControlPlaneError,
    CredentialResolutionError,
    DataPlaneError,
    DownstreamContentError,
    DownstreamToolError,
    EntityNotFoundError,
    IdempotencyConflictError,
    InputRequiredError,
    InvalidStateError,
    OperationCancelledError,
    OperationInProgressError,
    OperationNotFoundError,
    RateLimitExceededError,
    RequestTooLargeError,
    RequestValidationError,
    ServerDisabledError,
    UnknownOutcomeError,
    UnsafeEndpointError,
    UnsupportedPrimitiveError,
)
from ..domain.models import OperationRecord
from .runtime import AdapterRuntime, BearerAuthenticator, Principal

MAX_INGRESS_BYTES = 1024 * 1024


def create_http_app(
    *,
    runtime: AdapterRuntime,
    authenticator: BearerAuthenticator,
    stale_after_seconds: int = 300,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Starlette:
    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncGenerator[None]:
        cutoff = now() - timedelta(seconds=stale_after_seconds)
        await anyio.to_thread.run_sync(
            lambda: runtime.recover_stale(updated_before=cutoff.isoformat())
        )
        yield

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def openapi(request: Request) -> Response:
        principal = _principal(request, authenticator)
        server_id = request.query_params.get("serverId")
        if not server_id or server_id not in principal.server_ids:
            raise AuthorizationError("An authorized serverId query parameter is required")
        return JSONResponse(runtime.openapi(principal, server_id))

    async def execute(request: Request) -> Response:
        principal = _principal(request, authenticator)
        payload = await _payload(request)
        operation_id = request.path_params["public_operation_id"]
        result = await anyio.to_thread.run_sync(
            lambda: runtime.execute(principal, operation_id, payload)
        )
        return JSONResponse(response_to_dict(result))

    async def resume(request: Request) -> Response:
        principal = _principal(request, authenticator)
        payload = await _payload(request)
        operation_id = request.path_params["operation_id"]
        result = await anyio.to_thread.run_sync(
            lambda: runtime.resume(principal, operation_id, payload)
        )
        return JSONResponse(response_to_dict(result))

    async def cancel(request: Request) -> Response:
        principal = _principal(request, authenticator)
        payload = await _payload(request)
        reason = payload.get("reason", "Cancelled by caller")
        if not isinstance(reason, str) or not reason:
            raise RequestValidationError("Cancellation reason must be a non-empty string")
        operation_id = request.path_params["operation_id"]
        result = await anyio.to_thread.run_sync(
            lambda: runtime.cancel(principal, operation_id, reason=reason)
        )
        body = _operation_dict(result)
        if result.status == "CANCELLED":
            body["cancellation"] = "applied"
            return JSONResponse(body)
        # A dispatched write cannot be rolled back: the request is recorded, and the operation
        # still resolves to its real outcome (COMPLETED, FAILED, or UNKNOWN).
        body["cancellation"] = "requested"
        body["message"] = (
            "The operation is already running and cannot be aborted safely; "
            "poll its status for the final outcome"
        )
        return JSONResponse(body, status_code=202)

    async def operation(request: Request) -> Response:
        principal = _principal(request, authenticator)
        operation_id = request.path_params["operation_id"]
        result = await anyio.to_thread.run_sync(
            lambda: runtime.operation(principal, operation_id)
        )
        return JSONResponse(_operation_dict(result))

    async def metrics(request: Request) -> Response:
        principal = _principal(request, authenticator)
        if not principal.administrator:
            raise AuthorizationError("Administrator access is required")
        return JSONResponse(runtime.metrics.snapshot())

    async def artifact(request: Request) -> Response:
        principal = _principal(request, authenticator)
        artifact_id = request.path_params["artifact_id"]
        content, media_type = await anyio.to_thread.run_sync(
            lambda: runtime.artifact(principal, artifact_id)
        )
        return Response(content, media_type=media_type)

    async def error_handler(_: Request, error: Exception) -> Response:
        status = _status(error)
        payload: dict[str, object] = {
            "error": getattr(error, "code", type(error).__name__),
            "message": str(error),
        }
        if isinstance(error, (ApprovalRequiredError, InputRequiredError, UnknownOutcomeError)):
            payload["operationId"] = error.operation_id
        if isinstance(error, ApprovalRequiredError):
            payload["actionDigest"] = error.action_digest
        if isinstance(error, InputRequiredError):
            payload["schema"] = error.schema
        return JSONResponse(payload, status_code=status)

    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/openapi.json", openapi, methods=["GET"]),
            Route("/v1/operations/{public_operation_id}", execute, methods=["POST"]),
            Route("/v1/executions/{operation_id}", operation, methods=["GET"]),
            Route("/v1/executions/{operation_id}/resume", resume, methods=["POST"]),
            Route("/v1/executions/{operation_id}/cancel", cancel, methods=["POST"]),
            Route("/metrics", metrics, methods=["GET"]),
            Route("/v1/artifacts/{artifact_id}", artifact, methods=["GET"]),
        ],
        exception_handlers={
            AuthenticationError: error_handler,
            AuthorizationError: error_handler,
            DataPlaneError: error_handler,
            ControlPlaneError: error_handler,
        },
        lifespan=lifespan,
    )


def _principal(request: Request, authenticator: BearerAuthenticator) -> Principal:
    return authenticator.authenticate(request.headers.get("authorization"))


async def _payload(request: Request) -> Mapping[str, object]:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_INGRESS_BYTES:
        raise RequestTooLargeError("Request body exceeds the ingress size limit")
    body = await request.body()
    if len(body) > MAX_INGRESS_BYTES:
        raise RequestTooLargeError("Request body exceeds the ingress size limit")
    try:
        value: object = json.loads(body)
    except ValueError as error:
        raise RequestValidationError("Request body must be valid JSON") from error
    if not isinstance(value, dict):
        raise RequestValidationError("Request body must be a JSON object")
    return cast(Mapping[str, object], value)


def _operation_dict(operation: OperationRecord) -> dict[str, object]:
    """Public view of an operation; stored results and approval details stay internal."""
    return {
        "operationId": operation.operation_id,
        "requestId": operation.request_id,
        "serverId": operation.server_id,
        "catalogVersion": operation.catalog_version,
        "publicOperationId": operation.public_operation_id,
        "classification": operation.classification,
        "status": operation.status,
        "createdAt": operation.created_at,
        "updatedAt": operation.updated_at,
        "errorCode": operation.error_code,
        "errorMessage": operation.error_message,
        "retryable": operation.retryable,
    }


def _status(error: Exception) -> int:
    """Map adapter errors to HTTP statuses a Muse connector can act on."""
    if isinstance(error, AuthenticationError):
        return 401
    if isinstance(error, AuthorizationError | ServerDisabledError | CredentialResolutionError):
        return 403
    if isinstance(error, OperationNotFoundError | EntityNotFoundError):
        return 404
    if isinstance(error, RequestTooLargeError):
        return 413
    if isinstance(error, RateLimitExceededError):
        return 429
    if isinstance(
        error,
        ApprovalRequiredError
        | InputRequiredError
        | UnknownOutcomeError
        | IdempotencyConflictError
        | OperationInProgressError
        | OperationCancelledError
        | InvalidStateError
        | ConflictError,
    ):
        return 409
    if isinstance(
        error,
        DownstreamToolError | DownstreamContentError | UnsafeEndpointError,
    ):
        return 502
    if isinstance(error, RequestValidationError | UnsupportedPrimitiveError):
        return 400
    if isinstance(error, DataPlaneError):
        return 500
    return 400
