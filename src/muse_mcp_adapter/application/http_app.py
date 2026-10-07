from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..catalog.canonical import thaw_json
from ..data_plane.serde import response_to_dict
from ..domain.errors import (
    ApprovalRequiredError,
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    ControlPlaneError,
    CredentialResolutionError,
    DataPlaneError,
    DownstreamAuthRejectedError,
    DownstreamConnectionLostError,
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
    ReauthorizationRequiredError,
    RequestTooLargeError,
    RequestValidationError,
    ServerDisabledError,
    UnknownOutcomeError,
    UnsafeEndpointError,
    UnsupportedPrimitiveError,
)
from ..domain.models import OperationRecord
from .runtime import AdapterRuntime, Principal, payload_object

MAX_INGRESS_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 64


class WebAuthorization(Protocol):
    """Browser-facing routes of the delegated OAuth flow."""

    def routes(self) -> list[Route]: ...


def create_http_app(
    *,
    runtime: AdapterRuntime,
    web_authorization: Sequence[WebAuthorization] = (),
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

    def principal_of(request: Request) -> Principal:
        return runtime.authenticate(
            request.headers.get("authorization"), request.path_params["slug"]
        )

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def openapi(request: Request) -> Response:
        slug = request.path_params["slug"]
        document = await anyio.to_thread.run_sync(lambda: runtime.openapi(slug))
        return JSONResponse(document)

    async def execute(request: Request) -> Response:
        principal = principal_of(request)
        arguments = await _arguments(request)
        operation_id = request.path_params["public_operation_id"]
        key = request.headers.get("idempotency-key")
        request_id = _request_id(request)
        result = await anyio.to_thread.run_sync(
            lambda: runtime.execute(
                principal,
                operation_id,
                arguments,
                idempotency_key=key,
                request_id=request_id,
            )
        )
        return JSONResponse(response_to_dict(result))

    async def resume(request: Request) -> Response:
        principal = principal_of(request)
        payload = await _arguments(request)
        arguments = payload_object(payload.get("arguments"), "arguments")
        inputs = payload_object(payload.get("inputs"), "inputs")
        operation_id = request.path_params["operation_id"]
        result = await anyio.to_thread.run_sync(
            lambda: runtime.resume(principal, operation_id, arguments, inputs)
        )
        return JSONResponse(response_to_dict(result))

    async def operation(request: Request) -> Response:
        principal = principal_of(request)
        operation_id = request.path_params["operation_id"]
        result = await anyio.to_thread.run_sync(lambda: runtime.operation(principal, operation_id))
        events = await anyio.to_thread.run_sync(lambda: runtime.events(principal, operation_id))
        body = _operation_dict(result)
        body["events"] = [
            {"type": item.event_type, "at": item.occurred_at, "data": thaw_json(item.data)}
            for item in events
        ]
        return JSONResponse(body)

    async def artifact(request: Request) -> Response:
        principal = principal_of(request)
        artifact_id = request.path_params["artifact_id"]
        content, media_type = await anyio.to_thread.run_sync(
            lambda: runtime.artifact(principal, artifact_id)
        )
        return Response(content, media_type=media_type)

    async def error_handler(request: Request, error: Exception) -> Response:
        payload: dict[str, object] = {
            "error": getattr(error, "code", type(error).__name__),
            "message": str(error),
        }
        if isinstance(error, ApprovalRequiredError | InputRequiredError | UnknownOutcomeError):
            payload["operationId"] = error.operation_id
        if isinstance(error, ApprovalRequiredError):
            payload["actionDigest"] = error.action_digest
        if isinstance(error, InputRequiredError):
            payload["schema"] = thaw_json(error.schema)
            payload["nextStep"] = (
                "Do not repeat the original call. Ask the user for the fields in `schema`, then "
                "call resume_operation with this operationId and "
                '{"arguments": <the same arguments>, "inputs": <the answers>}.'
            )
        if isinstance(error, ReauthorizationRequiredError) and error.authorization_url:
            payload["authorizationUrl"] = error.authorization_url
        headers: dict[str, str] = {}
        if isinstance(error, AuthenticationError):
            # Lets an OAuth-capable client discover how to sign in (RFC 9728).
            challenge = 'Bearer realm="muse-mcp-adapter"'
            slug = request.path_params.get("slug")
            if slug:
                challenge += (
                    f', resource_metadata="{runtime.public_url}'
                    f'/.well-known/oauth-protected-resource/c/{slug}"'
                )
            headers["WWW-Authenticate"] = challenge
        return JSONResponse(payload, status_code=_status(error), headers=headers)

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/c/{slug}/openapi.json", openapi, methods=["GET"]),
        Route("/c/{slug}/operations/{public_operation_id}", execute, methods=["POST"]),
        Route("/c/{slug}/executions/{operation_id}", operation, methods=["GET"]),
        Route("/c/{slug}/executions/{operation_id}/resume", resume, methods=["POST"]),
        Route("/c/{slug}/artifacts/{artifact_id}", artifact, methods=["GET"]),
    ]
    for extra in web_authorization:
        routes.extend(extra.routes())
    return Starlette(
        routes=routes,
        exception_handlers={
            AuthenticationError: error_handler,
            AuthorizationError: error_handler,
            DataPlaneError: error_handler,
            ControlPlaneError: error_handler,
        },
        lifespan=lifespan,
    )


async def _arguments(request: Request) -> Mapping[str, object]:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_INGRESS_BYTES:
        raise RequestTooLargeError("Request body exceeds the size limit")
    body = await request.body()
    if len(body) > MAX_INGRESS_BYTES:
        raise RequestTooLargeError("Request body exceeds the size limit")
    if not body.strip():
        return {}
    try:
        value: object = json.loads(body)
    except (ValueError, RecursionError) as error:
        raise RequestValidationError("Request body must be valid JSON") from error
    if _nesting(value) > MAX_JSON_DEPTH:
        raise RequestValidationError("Request body is nested too deeply")
    return payload_object(value, "Request body")


def _nesting(value: object) -> int:
    """The deepest level of arrays and objects, measured without recursion."""
    deepest = 0
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        item, level = pending.pop()
        if isinstance(item, dict):
            children = list(cast(dict[str, object], item).values())
        elif isinstance(item, list):
            children = list(cast(list[object], item))
        else:
            continue
        deepest = max(deepest, level + 1)
        if deepest > MAX_JSON_DEPTH:
            return deepest
        pending.extend((child, level + 1) for child in children)
    return deepest


def _request_id(request: Request) -> str | None:
    value = request.headers.get("x-request-id")
    if value is None:
        return None
    if not 1 <= len(value) <= 200 or not value.isprintable():
        raise RequestValidationError("X-Request-Id must be 1 to 200 printable characters")
    return value


def _operation_dict(operation: OperationRecord) -> dict[str, object]:
    """Public view of an operation; stored results and approval details stay internal."""
    return {
        "operationId": operation.operation_id,
        "requestId": operation.request_id,
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
    if isinstance(error, AuthenticationError | ReauthorizationRequiredError):
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
        DownstreamToolError
        | DownstreamContentError
        | DownstreamAuthRejectedError
        | DownstreamConnectionLostError
        | UnsafeEndpointError,
    ):
        return 502
    if isinstance(error, RequestValidationError | UnsupportedPrimitiveError):
        return 400
    if isinstance(error, DataPlaneError):
        return 500
    return 400


__all__ = ["WebAuthorization", "create_http_app", "payload_object"]
_ = cast
