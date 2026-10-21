"""An HTTP error from an MCP server becomes a sentence, not a stack trace."""

import httpx
import pytest

from muse_mcp_adapter.domain.errors import (
    DownstreamAuthRejectedError,
    DownstreamToolError,
    DownstreamUnavailableError,
)
from muse_mcp_adapter.transport.sdk_client import _run  # pyright: ignore[reportPrivateUsage]


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://mcp.example.com/mcp")
    return httpx.HTTPStatusError(
        f"HTTP {code}", request=request, response=httpx.Response(code, request=request)
    )


def _raising(error: BaseException):  # type: ignore[no-untyped-def]
    async def call() -> None:
        raise BaseExceptionGroup("task group", [error])  # as the MCP SDK's task groups do

    return call


def test_a_400_to_the_first_request_is_reported_as_a_bad_token() -> None:
    with pytest.raises(DownstreamAuthRejectedError, match="HTTP 400.*not the right kind of token"):
        _run(_raising(_status_error(400)))


@pytest.mark.parametrize("code", [401, 403])
def test_a_rejected_token_is_still_reported_as_rejected(code: int) -> None:
    with pytest.raises(DownstreamAuthRejectedError, match=f"HTTP {code}"):
        _run(_raising(_status_error(code)))


def test_a_404_says_nothing_answers_as_an_mcp_server_there() -> None:
    with pytest.raises(DownstreamToolError, match="HTTP 404 at mcp.example.com.*Check the address"):
        _run(_raising(_status_error(404)))


def test_a_server_error_is_reported_as_the_servers_problem_and_can_be_retried() -> None:
    with pytest.raises(DownstreamUnavailableError, match="HTTP 503"):
        _run(_raising(_status_error(503)))
