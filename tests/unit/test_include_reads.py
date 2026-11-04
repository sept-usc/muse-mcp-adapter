"""includeReads: every read, plus what is named, less what is excluded."""

from __future__ import annotations

from typing import Any

import pytest

from muse_mcp_adapter.chat_setup.compile import compile_proposal
from muse_mcp_adapter.chat_setup.models import REVISE_FIELDS, SetupRequest
from muse_mcp_adapter.domain.errors import RequestValidationError
from tests.unit.test_chat_setup_compile import PRIMITIVES


def _selected(**fields: Any) -> set[str]:
    request = SetupRequest.parse(
        {"endpoint": "https://t.example/mcp", "userWords": "w", **fields}, change=False
    )
    computed = compile_proposal(
        primitives=PRIMITIVES, request=request, current=None, current_digests=None,
        snapshot_digest="sha256:x", base_version=None,
    )  # fmt: skip
    return {c.name for c in computed.candidates if c.selected and c.kind == "tool"}


def test_nothing_is_selected_without_a_reason() -> None:
    assert _selected() == set()


def test_include_reads_selects_the_reads_and_only_those() -> None:
    assert _selected(includeReads=True) == {"list_issues", "get_issue"}


def test_include_reads_adds_what_is_named() -> None:
    assert _selected(includeReads=True, alsoAllow=["create_issue"]) == {
        "list_issues", "get_issue", "create_issue",
    }  # fmt: skip


def test_it_behaves_like_read_only_when_nothing_else_is_named() -> None:
    assert _selected(includeReads=True) == _selected(readOnly=True)


def test_an_exclusion_beats_it() -> None:
    assert _selected(includeReads=True, exclude=["get_issue"]) == {"list_issues"}


def test_it_is_part_of_the_stored_request_and_parses_back() -> None:
    request = SetupRequest.parse(
        {"endpoint": "https://t.example/mcp", "userWords": "w", "includeReads": True}, change=False
    )
    assert request.include_reads is True
    again = SetupRequest.parse(request.to_json(), change=False)
    assert again == request and request.to_json()["includeReads"] is True


@pytest.mark.parametrize("bad", ["yes", 1, [], {}])
def test_it_must_be_true_or_false(bad: object) -> None:
    with pytest.raises(RequestValidationError, match="includeReads must be true or false"):
        SetupRequest.parse(
            {"endpoint": "https://t.example/mcp", "userWords": "w", "includeReads": bad},
            change=False,
        )


def test_a_change_request_accepts_it_too() -> None:
    request = SetupRequest.parse(
        {"connectionId": "conn_0123456789ab", "userWords": "w", "includeReads": True}, change=True
    )
    assert request.include_reads


def test_a_revision_may_change_these_fields_and_never_the_service_or_the_words() -> None:
    assert {
        "includeReads",
        "alsoAllow",
        "exclude",
        "readOnly",
        "expiresInHours",
        "limitArguments",
    } <= set(REVISE_FIELDS)
    assert not {"server", "endpoint", "userWords", "connectionId"} & set(REVISE_FIELDS)
