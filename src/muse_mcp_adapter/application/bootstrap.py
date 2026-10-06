from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from starlette.applications import Starlette

from ..connections import ConnectionService
from ..credentials.resolver import CredentialResolver
from ..data_plane.approval import ApprovalVerifier
from ..data_plane.mcp_client import McpReadClient
from ..data_plane.read_service import ReadDataPlaneService
from ..data_plane.tool_client import McpToolClient
from ..data_plane.tool_service import ToolDataPlaneService
from ..operations.artifacts import FileArtifactStore
from ..operations.cancellation import CancellationRegistry
from ..operations.lifecycle import OperationLifecycleService
from ..operations.runtime_controls import (
    AdapterMetrics,
    FixedWindowRateLimiter,
    RequestConcurrencyLimiter,
)
from ..persistence.sqlite_repository import SqliteControlPlaneRepository
from .http_app import WebAuthorization, create_http_app
from .oauth_web import OAuthWebFlow
from .runtime import AdapterRuntime


def build_http_application(
    *,
    repository: SqliteControlPlaneRepository,
    credential_resolver: CredentialResolver,
    mcp_client: McpReadClient | McpToolClient,
    artifact_root: Path,
    public_url: str,
    clock: Callable[[], str],
    approval_verifier: ApprovalVerifier | None = None,
    oauth_flow: OAuthWebFlow | None = None,
    rate_limit: int = 60,
    rate_window_seconds: float = 60,
    per_server_concurrency: int = 8,
    idempotency_window_seconds: int = 300,
) -> Starlette:
    """Build the supported request path so its controls cannot be left disconnected."""
    repository.initialize()
    cancellations = CancellationRegistry()
    artifacts = FileArtifactStore(artifact_root, repository, clock=clock)
    reads = ReadDataPlaneService(
        repository,
        credential_resolver,
        mcp_client,  # type: ignore[arg-type]
    )
    tools = ToolDataPlaneService(
        repository,
        credential_resolver,
        mcp_client,  # type: ignore[arg-type]
        approval_verifier=approval_verifier,
        clock=clock,
        cancellation_registry=cancellations,
        artifact_store=artifacts,
    )
    lifecycle = OperationLifecycleService(
        repository, clock=clock, cancellation_registry=cancellations
    )
    runtime = AdapterRuntime(
        repository=repository,
        connections=ConnectionService(repository, clock=clock),
        read_service=reads,
        tool_service=tools,
        lifecycle=lifecycle,
        rate_limiter=FixedWindowRateLimiter(limit=rate_limit, window_seconds=rate_window_seconds),
        concurrency_limiter=RequestConcurrencyLimiter(per_key_limit=per_server_concurrency),
        metrics=AdapterMetrics(),
        clock=clock,
        public_url=public_url,
        artifact_store=artifacts,
        idempotency_window_seconds=idempotency_window_seconds,
        reauthorization_link=oauth_flow.login_link if oauth_flow is not None else None,
    )
    web: WebAuthorization | None = oauth_flow
    return create_http_app(runtime=runtime, web_authorization=web)
