from collections.abc import Iterator

import pytest

from muse_mcp_adapter.domain.errors import RateLimitExceededError, UnsafeEndpointError
from muse_mcp_adapter.operations.runtime_controls import (
    AdapterMetrics,
    FixedWindowRateLimiter,
    RequestConcurrencyLimiter,
)
from muse_mcp_adapter.transport.security import EndpointSecurityPolicy


def test_rate_limit_resets_and_metrics_are_snapshot_safe() -> None:
    times: Iterator[float] = iter([0.0, 0.1, 0.2, 11.0])
    limiter = FixedWindowRateLimiter(limit=2, window_seconds=10, clock=lambda: next(times))
    limiter.acquire("connection-a")
    limiter.acquire("connection-a")
    with pytest.raises(RateLimitExceededError):
        limiter.acquire("connection-a")
    limiter.acquire("connection-a")

    metrics = AdapterMetrics()
    metrics.increment("operations.completed")
    metrics.increment("operations.completed", value=2)
    assert metrics.snapshot() == {"operations.completed": 3}


def test_concurrency_limit_fails_closed_without_waiting() -> None:
    limiter = RequestConcurrencyLimiter(per_key_limit=1)
    with (
        limiter.slot("server-a"),
        pytest.raises(RateLimitExceededError),
        limiter.slot("server-a"),
    ):
        raise AssertionError("unreachable")


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://mcp.example.test/mcp",
        "https://localhost/mcp",
        "https://127.0.0.1/mcp",
        "https://user:password@mcp.example.test/mcp",
    ],
)
def test_remote_endpoint_policy_rejects_unsafe_targets(endpoint: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        EndpointSecurityPolicy().validate(endpoint)


def test_private_endpoint_requires_explicit_deployment_opt_in() -> None:
    EndpointSecurityPolicy(allow_private_networks=True).validate("https://127.0.0.1/mcp")
