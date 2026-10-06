# pyright: basic
"""Discovery against servers that page their listings or misbehave while doing so."""

from collections.abc import Sequence
from types import SimpleNamespace

import anyio
import pytest

from muse_mcp_adapter.domain.errors import InvalidStateError
from muse_mcp_adapter.transport.sdk_client import _all_pages

pytestmark = pytest.mark.validation


def _pages(pages: Sequence[tuple[list[int], str | None]]):  # type: ignore[no-untyped-def]
    calls: list[str | None] = []

    async def method(cursor: str | None) -> SimpleNamespace:
        calls.append(cursor)
        items, following = pages[len(calls) - 1]
        return SimpleNamespace(tools=items, nextCursor=following)

    return method, calls


def test_every_page_of_a_long_listing_is_collected() -> None:
    method, calls = _pages([([1, 2], "a"), ([3, 4], "b"), ([5], None)])
    assert anyio.run(_all_pages, method, "tools") == [1, 2, 3, 4, 5]
    assert calls == [None, "a", "b"]


def test_a_thousand_pages_are_followed_to_the_end() -> None:
    method, _ = _pages([([n], str(n)) for n in range(1000)] + [([1000], None)])
    assert len(anyio.run(_all_pages, method, "tools")) == 1001


def test_a_server_that_repeats_a_cursor_is_stopped_instead_of_looping_forever() -> None:
    method, _ = _pages([([1], "same"), ([2], "same"), ([3], "same")])
    with pytest.raises(InvalidStateError, match="repeating"):
        anyio.run(_all_pages, method, "tools")


def test_an_empty_listing_is_an_empty_catalog() -> None:
    method, _ = _pages([([], None)])
    assert anyio.run(_all_pages, method, "tools") == []
