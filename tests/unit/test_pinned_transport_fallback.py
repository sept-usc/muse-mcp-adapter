# pyright: basic
"""A hostname with several checked addresses: use the first that answers, and remember it."""

import asyncio
from typing import Any

import httpx
import pytest

from muse_mcp_adapter.transport.sdk_client import FALLBACK_CONNECT_SECONDS, _PinnedAsyncTransport
from muse_mcp_adapter.transport.security import EndpointSecurityPolicy, ResolvedEndpoint


class _Scripted(httpx.AsyncBaseTransport):
    """Answers per address: an exception to raise, or a status code."""

    def __init__(self, behaviour: dict[str, Any]) -> None:
        self.behaviour = behaviour
        self.tried: list[str] = []
        self.timeouts: list[Any] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        address = request.url.host
        self.tried.append(address)
        self.timeouts.append(request.extensions.get("timeout"))
        outcome = self.behaviour[address]
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, request=request)


def _transport(inner: _Scripted, addresses: tuple[str, ...]) -> _PinnedAsyncTransport:
    endpoint = "http://mcp.example.com:80/mcp"
    target = ResolvedEndpoint(hostname="mcp.example.com", port=80, addresses=addresses)
    return _PinnedAsyncTransport(inner, EndpointSecurityPolicy(), endpoint, target)


def _get(transport: _PinnedAsyncTransport) -> httpx.Response:
    request = httpx.Request("GET", "http://mcp.example.com/mcp")
    return asyncio.run(transport.handle_async_request(request))


def test_an_unreachable_first_address_falls_back_to_the_next() -> None:
    inner = _Scripted({"2001:db8::1": httpx.ConnectError("no route"), "203.0.113.7": 200})
    transport = _transport(inner, ("2001:db8::1", "203.0.113.7"))
    assert _get(transport).status_code == 200
    assert inner.tried == ["2001:db8::1", "203.0.113.7"]


def test_the_address_that_worked_is_tried_first_next_time() -> None:
    inner = _Scripted({"2001:db8::1": httpx.ConnectTimeout("slow"), "203.0.113.7": 200})
    transport = _transport(inner, ("2001:db8::1", "203.0.113.7"))
    _get(transport)
    inner.tried.clear()
    _get(transport)
    assert inner.tried == ["203.0.113.7"]


def test_an_address_that_does_not_answer_is_abandoned_quickly_when_another_is_left() -> None:
    inner = _Scripted({"2001:db8::1": httpx.ConnectTimeout("slow"), "203.0.113.7": 200})
    _get(_transport(inner, ("2001:db8::1", "203.0.113.7")))
    assert inner.timeouts[0]["connect"] == FALLBACK_CONNECT_SECONDS  # first: another remains
    assert inner.timeouts[1] is None or "connect" not in (inner.timeouts[1] or {})  # last: normal


def test_when_every_address_fails_the_last_error_is_raised() -> None:
    inner = _Scripted(
        {"198.51.100.1": httpx.ConnectError("a"), "203.0.113.7": httpx.ConnectError("b")}
    )
    with pytest.raises(httpx.ConnectError, match="b"):
        _get(_transport(inner, ("198.51.100.1", "203.0.113.7")))
    assert inner.tried == ["198.51.100.1", "203.0.113.7"]


def test_a_failure_after_connecting_is_not_retried_on_another_address() -> None:
    """Only a failure to connect is safe to retry: once bytes may have been sent, it is not."""
    inner = _Scripted({"198.51.100.1": httpx.ReadError("reset"), "203.0.113.7": 200})
    with pytest.raises(httpx.ReadError):
        _get(_transport(inner, ("198.51.100.1", "203.0.113.7")))
    assert inner.tried == ["198.51.100.1"]


def test_only_checked_addresses_are_ever_used() -> None:
    inner = _Scripted({"203.0.113.7": 200})
    assert _get(_transport(inner, ("203.0.113.7",))).status_code == 200
    assert inner.tried == ["203.0.113.7"]  # never the hostname: no second DNS lookup
