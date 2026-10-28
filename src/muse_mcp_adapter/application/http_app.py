from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

import anyio
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..catalog.canonical import thaw_json
from ..data_plane.serde import response_to_dict
from ..domain.errors import (
    ApprovalDeniedError,
    ApprovalRequiredError,
    ArgumentNotAllowedError,
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
    DownstreamUnavailableError,
    EntityNotFoundError,
    HandleNotAllowedError,
    HandleUnknownError,
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
from ..domain.models import OperationEvent, OperationRecord
from .runtime import AdapterRuntime, Principal, payload_object

MAX_INGRESS_BYTES = 1024 * 1024

# Added to every response unless a route already set the header itself. The adapter is always
# served over HTTPS by whatever sits in front of it, so it states the policy itself instead of
# relying on that proxy to.
SECURITY_HEADERS = (
    (b"strict-transport-security", b"max-age=31536000"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
)


class SecurityHeaders:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def add(message: Message) -> None:
            if message["type"] == "http.response.start":
                present = {name.lower() for name, _ in message.get("headers", [])}
                message["headers"] = [
                    *message.get("headers", []),
                    *((n, v) for n, v in SECURITY_HEADERS if n not in present),
                ]
            await send(message)

        await self.app(scope, receive, add)


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
        body = response_to_dict(result)
        if result.operation_id and not result.replayed:
            body.update(
                await anyio.to_thread.run_sync(
                    lambda: _server_notes(runtime.events(principal, result.operation_id or ""))
                )
            )
        return JSONResponse(body)

    async def reveal(request: Request) -> Response:
        principal = principal_of(request)
        arguments = await _arguments(request)
        result = await anyio.to_thread.run_sync(
            lambda: runtime.reveal(principal, arguments, request_id=_request_id(request))
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
            if error.approval_url:
                payload["approvalUrl"] = error.approval_url
            if error.reasons:
                payload["reasons"] = list(error.reasons)
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
        advice = retry_guidance(error)
        payload["retryable"] = advice.retryable
        payload.setdefault("nextStep", advice.next_step)
        if advice.retry_after is not None:
            headers["Retry-After"] = str(advice.retry_after)
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
        Route("/c/{slug}/operations/reveal_value", reveal, methods=["POST"]),
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
        middleware=[Middleware(SecurityHeaders)],
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


def _server_notes(events: Sequence[OperationEvent]) -> dict[str, object]:
    """The progress and log messages the server sent while working, as part of the result."""
    notes: dict[str, object] = {}
    progress = [thaw_json(item.data) for item in events if item.event_type == "progress"]
    logs = [thaw_json(item.data) for item in events if item.event_type == "log"]
    if progress:
        notes["progress"] = progress
    if logs:
        notes["logs"] = logs
    return notes


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


@dataclass(frozen=True)
class RetryGuidance:
    """What a caller should do next. Muse retries what it is not told is final, so say it."""

    retryable: bool
    next_step: str
    retry_after: int | None = None


def retry_guidance(error: Exception) -> RetryGuidance:
    if isinstance(error, RateLimitExceededError):
        return RetryGuidance(
            True, "Too many requests. Wait a few seconds, then repeat the same call.", 5
        )
    if isinstance(error, DownstreamUnavailableError | DownstreamConnectionLostError):
        return RetryGuidance(
            True,
            "The MCP server could not be reached and nothing was done. Repeating the same call "
            "is safe; if it keeps failing, tell the user the server is unavailable.",
            5,
        )
    if isinstance(error, UnknownOutcomeError):
        return RetryGuidance(
            False,
            "Do not repeat this call: it may already have happened. Tell the user, who can "
            "check the other system and settle the operation.",
        )
    if isinstance(error, OperationInProgressError):
        return RetryGuidance(
            False,
            "The same request is already running. Wait, then check its status; do not resend.",
        )
    if isinstance(error, DownstreamToolError):
        return RetryGuidance(
            False,
            "The MCP server refused or failed this call. Do not repeat it unchanged: tell the "
            "user what the message says.",
        )
    if isinstance(error, DownstreamAuthRejectedError | CredentialResolutionError):
        return RetryGuidance(
            False,
            "The adapter's credential for this server was rejected or is missing. Repeating will "
            "not help: tell the user the owner must renew it.",
        )
    if isinstance(error, ReauthorizationRequiredError):
        return RetryGuidance(
            False,
            "The owner must sign in again (see authorizationUrl), then the call can be repeated.",
        )
    if isinstance(error, AuthenticationError | AuthorizationError | ServerDisabledError):
        return RetryGuidance(
            False, "This connector is not allowed to do that. Do not repeat the call."
        )
    if isinstance(error, RequestValidationError | UnsupportedPrimitiveError | RequestTooLargeError):
        return RetryGuidance(
            False, "The request is invalid. Fix the arguments before trying again."
        )
    if isinstance(error, ApprovalDeniedError | ArgumentNotAllowedError):
        return RetryGuidance(False, "The owner does not allow this. Do not repeat the call.")
    if isinstance(error, HandleNotAllowedError | HandleUnknownError):
        return RetryGuidance(
            False,
            "A sealed value was used where it is not allowed or was not recognised. Do not repeat "
            "the call as it is: take the value from a fresh result, use it only where the message "
            "says it may go, or leave it out.",
        )
    if isinstance(error, ApprovalRequiredError | InputRequiredError):
        return RetryGuidance(False, "More is needed from the user before this can run.")
    if isinstance(error, OperationNotFoundError | EntityNotFoundError):
        return RetryGuidance(False, "There is no such operation. Do not repeat the call.")
    if isinstance(error, DataPlaneError) and _status(error) >= 500:
        return RetryGuidance(True, "An internal error occurred. Repeating once may succeed.", 5)
    return RetryGuidance(False, "Do not repeat this call unchanged.")


def _status(error: Exception) -> int:
    """Map adapter errors to HTTP statuses a Muse connector can act on."""
    if isinstance(error, AuthenticationError | ReauthorizationRequiredError):
        return 401
    if isinstance(
        error,
        AuthorizationError
        | ServerDisabledError
        | CredentialResolutionError
        | ApprovalDeniedError
        | ArgumentNotAllowedError
        | HandleNotAllowedError,
    ):
        return 403
    if isinstance(error, OperationNotFoundError | EntityNotFoundError):
        return 404
    if isinstance(error, RequestTooLargeError):
        return 413
    if isinstance(error, RateLimitExceededError):
        return 429
    if isinstance(error, DownstreamUnavailableError):
        return 503
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
    if isinstance(error, RequestValidationError | UnsupportedPrimitiveError | HandleUnknownError):
        return 400
    if isinstance(error, DataPlaneError):
        return 500
    return 400


__all__ = ["WebAuthorization", "create_http_app", "payload_object"]
_ = cast
