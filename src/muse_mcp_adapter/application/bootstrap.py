from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from starlette.applications import Starlette

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
from .http_app import create_http_app
from .runtime import AdapterRuntime, BearerAuthenticator, Principal


def build_http_application(
    *,
    repository: SqliteControlPlaneRepository,
    credential_resolver: CredentialResolver,
    mcp_client: McpReadClient | McpToolClient,
    approval_verifier: ApprovalVerifier,
    ingress_tokens: Mapping[str, Principal],
    artifact_root: Path,
    clock: Callable[[], str],
    rate_limit: int = 60,
    rate_window_seconds: float = 60,
    per_server_concurrency: int = 8,
) -> Starlette:
    """Build the supported production graph; controls cannot be accidentally left disconnected."""
    repository.initialize()
    cancellations = CancellationRegistry()
    artifacts = FileArtifactStore(artifact_root, repository, clock=clock)
    reads = ReadDataPlaneService(
        repository, credential_resolver, mcp_client  # type: ignore[arg-type]
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
        repository,
        clock=clock,
        cancellation_registry=cancellations,
    )
    runtime = AdapterRuntime(
        repository=repository,
        read_service=reads,
        tool_service=tools,
        lifecycle=lifecycle,
        rate_limiter=FixedWindowRateLimiter(
            limit=rate_limit, window_seconds=rate_window_seconds
        ),
        concurrency_limiter=RequestConcurrencyLimiter(
            per_key_limit=per_server_concurrency
        ),
        metrics=AdapterMetrics(),
        clock=clock,
        artifact_store=artifacts,
    )
    return create_http_app(
        runtime=runtime,
        authenticator=BearerAuthenticator(ingress_tokens),
    )
