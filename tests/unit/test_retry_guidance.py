"""Every error tells the caller whether repeating the call can help."""

import pytest

from muse_mcp_adapter.application import http_app
from muse_mcp_adapter.application.http_app import retry_guidance
from muse_mcp_adapter.domain import errors as e

RETRYABLE = [
    (e.RateLimitExceededError("slow down"), 429),
    (e.DownstreamUnavailableError("cannot connect"), 503),
    (e.DownstreamConnectionLostError("stream closed"), 502),
]
FINAL = [
    e.DownstreamToolError("403 Resource not accessible"),
    e.DownstreamAuthRejectedError("credential rejected"),
    e.CredentialResolutionError("no secret"),
    e.UnknownOutcomeError("may have happened", operation_id="op"),
    e.OperationInProgressError("running"),
    e.AuthenticationError("bad token"),
    e.AuthorizationError("disabled"),
    e.RequestValidationError("bad arguments"),
    e.OperationNotFoundError("none"),
    e.ReauthorizationRequiredError("sign in again"),
    e.DownstreamContentError("Resource exceeds the published size limit of 262144 bytes"),
    e.PublishedPolicyError("Published timeoutSeconds is invalid"),
]


@pytest.mark.parametrize(("error", "status"), RETRYABLE)
def test_a_transient_failure_says_it_is_safe_to_repeat_and_when(
    error: Exception, status: int
) -> None:
    advice = retry_guidance(error)
    assert advice.retryable is True and advice.retry_after
    assert http_app._status(error) == status  # type: ignore[reportPrivateUsage]


@pytest.mark.parametrize("error", FINAL, ids=lambda item: type(item).__name__)
def test_a_final_failure_says_not_to_repeat_it_unchanged(error: Exception) -> None:
    advice = retry_guidance(error)
    assert advice.retryable is False and advice.retry_after is None and advice.next_step


def test_an_unreachable_server_is_not_confused_with_a_server_that_said_no() -> None:
    assert e.DownstreamUnavailableError.code == "DOWNSTREAM_UNAVAILABLE"
    assert e.DownstreamUnavailableError.code != e.DownstreamToolError.code
    assert retry_guidance(e.DownstreamUnavailableError("x")).retryable
    assert not retry_guidance(e.DownstreamToolError("x")).retryable


def test_an_unknown_error_defaults_to_do_not_repeat() -> None:
    assert retry_guidance(ValueError("surprise")).retryable is False


def test_a_rejected_result_says_repeating_gets_the_same_result_and_what_to_do_instead() -> None:
    """Found with a real Muse: a page over the size limit was answered "An internal error
    occurred. Repeating once may succeed.", and the same page would be rejected again."""
    error = e.DownstreamContentError("Resource exceeds the published size limit of 262144 bytes")
    advice = retry_guidance(error)
    assert advice.retryable is False
    assert "same result" in advice.next_step and "ask for less" in advice.next_step
    assert "internal error" not in advice.next_step.lower()
    assert http_app._status(error) == 502  # type: ignore[reportPrivateUsage]
