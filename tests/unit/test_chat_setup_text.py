"""Text an agent supplies is read by the owner, so what the owner reads must be what is stored."""

from __future__ import annotations

import pytest

from muse_mcp_adapter.chat_setup.models import SetupRequest, parse_limits
from muse_mcp_adapter.domain.errors import RequestValidationError


def _parse(**fields: object) -> SetupRequest:
    return SetupRequest.parse({"server": "learn", "userWords": "words", **fields}, change=False)


def test_hidden_direction_marks_and_zero_width_characters_are_dropped() -> None:
    request = _parse(userWords="approve \u202eyldnO daeR\u202c now\u200b!\u2066x\u2069")

    assert request.user_words == "approve yldnO daeR now!x"


def test_controls_become_spaces_but_tabs_and_line_breaks_stay() -> None:
    request = _parse(userWords="one\ntwo\tthree\x1b[31m red\x00end\u2028x", notes="a\x7fb")

    assert request.user_words == "one\ntwo\tthree [31m red end x"
    assert request.notes == "a b"


def test_a_limit_value_or_pattern_with_hidden_characters_is_refused_not_cleaned() -> None:
    for item in (
        {"allowedValues": ["EN\u202eG"]},
        {"allowedValues": ["ENG\u200b"]},
        {"pattern": "ENG\u200b.*"},
        {"pattern": "a\x00b"},
    ):
        limit = [{"operation": "*", "argument": "project", **item}]
        with pytest.raises(RequestValidationError, match="invisible"):
            parse_limits(limit)


def test_an_address_with_hidden_characters_is_refused() -> None:
    for address in ("https://example.com/mcp\u202e", "https://exa\u200bmple.com/mcp"):
        with pytest.raises(RequestValidationError, match="https address"):
            SetupRequest.parse({"endpoint": address, "userWords": "x"}, change=False)
