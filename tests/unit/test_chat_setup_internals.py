"""The corners of chat setup's compiler, request reader and storage that no page or call reaches.

A stored proposal can be damaged, a race can change a proposal between two steps, and a limit can
meet a prompt or an address template that takes the same argument. Each case is one of those.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from muse_mcp_adapter.chat_setup.compile import (
    Candidate,
    Computed,
    compile_proposal,
    finalize,
    not_held_to_limits,
)
from muse_mcp_adapter.chat_setup.models import SetupRequest, parse_limits
from muse_mcp_adapter.chat_setup.store import Proposal, SetupStore
from muse_mcp_adapter.domain.errors import ConflictError, InvalidStateError, RequestValidationError
from tests.unit.test_chat_setup_compile import (
    PRIMITIVES,
    _compile,  # pyright: ignore[reportPrivateUsage]
    _request,  # pyright: ignore[reportPrivateUsage]
)
from tests.unit.test_chat_setup_store import (
    LATER,
    NOW,
    _open,  # pyright: ignore[reportPrivateUsage]
    _store,  # pyright: ignore[reportPrivateUsage]
)

PROJECT = {"project": "(?:ENG)"}


def _tool(limits: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {"type": "tool", "name": "list_issues"},
        {"argumentPatterns": dict(limits)},
    )


# --- operations a limit does not reach ---------------------------------------------------


def test_a_prompt_or_template_that_takes_a_limited_argument_is_named() -> None:
    tool_index, tool_policy = _tool(PROJECT)
    index = {
        "tool:list_issues": tool_index,
        "prompt:triage": {"type": "prompt", "name": "triage", "arguments": [{"name": "project"}]},
        "resource_template:docs": {
            "type": "resource_template",
            "name": "docs",
            "uriTemplate": "mcp://docs/{project}/page",
        },
    }
    policies = {"tool:list_issues": tool_policy, "prompt:triage": {}, "resource_template:docs": {}}

    notes = not_held_to_limits(policies, index, set(index))

    assert len(notes) == 2
    assert notes[0].startswith("docs is an address template that takes project.")
    assert notes[1].startswith("triage is a prompt that takes project.")
    assert all("reaches tools only" in note for note in notes)


def test_only_what_the_owner_chose_is_named() -> None:
    tool_index, tool_policy = _tool(PROJECT)
    index = {
        "tool:list_issues": tool_index,
        "prompt:triage": {"type": "prompt", "name": "triage", "arguments": [{"name": "project"}]},
    }
    policies = {"tool:list_issues": tool_policy, "prompt:triage": {}}

    assert not_held_to_limits(policies, index, {"tool:list_issues"}) == []


def test_a_prompt_with_nothing_to_say_about_its_arguments_is_not_named() -> None:
    tool_index, tool_policy = _tool(PROJECT)
    index = {
        "tool:list_issues": tool_index,
        "prompt:none": {"type": "prompt", "name": "none"},
        "prompt:text": {"type": "prompt", "name": "text", "arguments": "project"},
        "prompt:junk": {"type": "prompt", "name": "junk", "arguments": ["project", 3, None]},
        "prompt:other": {"type": "prompt", "name": "other", "arguments": [{"name": "page"}]},
    }
    policies = {"tool:list_issues": tool_policy}

    assert not_held_to_limits(policies, index, set(index)) == []


@pytest.mark.parametrize(
    ("template", "names"),
    [
        ("mcp://x/{project}", "project"),
        ("mcp://x/{+path*}", "path"),
        ("mcp://x{/seg*}", "seg"),
        ("mcp://x{?limit:3}", "limit"),
        ("mcp://x{?a,limit:3,path*}", "limit, path"),
        ("mcp://x/{#frag}{;matrix}", "matrix"),
    ],
)
def test_the_variables_of_an_address_template_are_read_in_every_form(
    template: str, names: str
) -> None:
    limits = {name: "(?:x)" for name in ("project", "path", "seg", "limit", "matrix")}
    tool_index, tool_policy = _tool(limits)
    index = {
        "tool:t": tool_index,
        "resource_template:r": {"type": "resource_template", "name": "r", "uriTemplate": template},
    }

    notes = not_held_to_limits(
        {"tool:t": tool_policy, "resource_template:r": {}}, index, set(index)
    )

    assert notes == [
        f"r is an address template that takes {names}. A limit reaches tools only, so it is "
        "not held to it: switch it off if it must not reach other values."
    ]


def test_a_template_with_no_readable_address_takes_nothing() -> None:
    tool_index, tool_policy = _tool(PROJECT)
    index = {
        "tool:t": tool_index,
        "resource_template:r": {"type": "resource_template", "name": "r", "uriTemplate": 5},
        "resource_template:s": {"type": "resource_template", "name": "s"},
    }
    assert not_held_to_limits({"tool:t": tool_policy}, index, set(index)) == []


def test_a_long_list_of_such_operations_is_cut_short_and_says_so() -> None:
    tool_index, tool_policy = _tool(PROJECT)
    index: dict[str, Any] = {"tool:t": tool_index}
    for number in range(13):
        index[f"prompt:p{number:02d}"] = {
            "type": "prompt",
            "name": f"p{number:02d}",
            "arguments": [{"name": "project"}],
        }

    notes = not_held_to_limits({"tool:t": tool_policy}, index, set(index))

    assert len(notes) == 11
    assert notes[-1] == "...and 3 more like these."
    assert notes[0].startswith("p00 ")


# --- what the page says about how an operation was classed -------------------------------


def test_an_operation_the_owner_checked_is_said_to_have_been_checked() -> None:
    first = _compile(_request(readOnly=True))
    live = {key: dict(entry) for key, entry in first.policies.items() if entry["allowed"]}
    live["tool:search_notes"] = {
        **first.policies["tool:search_notes"],
        "allowed": True,
        "classification": "read",
        "readOnlyReviewed": True,
    }
    change = SetupRequest.parse(
        {"connectionId": "conn_0123456789ab", "userWords": "more time", "expiresInHours": 1},
        change=True,
    )

    computed = compile_proposal(
        primitives=PRIMITIVES,
        request=change,
        current=live,
        current_digests=None,
        snapshot_digest="sha256:snap",
        base_version="1",
    )

    notes = next(item for item in computed.candidates if item.name == "search_notes")
    assert notes.classification == "read"
    assert notes.evidence == "The owner checked that it only reads."


# --- a stored proposal that cannot be read -----------------------------------------------


def test_what_was_stored_reads_back_the_way_it_went_in() -> None:
    computed = _compile(_request(readOnly=True, expiresInHours=3))

    assert Computed.from_json(computed.to_json()) == computed
    assert [Candidate.from_json(item.to_json()) for item in computed.candidates] == list(
        computed.candidates
    )


@pytest.mark.parametrize("missing", ["key", "name", "kind", "classification", "evidence"])
def test_a_stored_candidate_missing_a_field_is_refused(missing: str) -> None:
    body = dict(_compile(_request(readOnly=True)).candidates[0].to_json())
    del body[missing]
    with pytest.raises(InvalidStateError, match=missing):
        Candidate.from_json(body)


@pytest.mark.parametrize(
    "damage",
    [
        {"candidates": "not a list"},
        {"candidates": [5]},
        {"policies": "not a mapping"},
        {"policies": {"tool:x": "not a mapping"}},
        {"unresolved": "no"},
        {"digest": 5},
        {"candidates": [{"limits": "no"}]},
    ],
)
def test_a_damaged_stored_proposal_is_refused_and_not_guessed_at(damage: dict[str, Any]) -> None:
    body = dict(_compile(_request(readOnly=True)).to_json())
    body.update(damage)
    with pytest.raises(InvalidStateError):
        Computed.from_json(body)


def test_a_stored_proposal_from_before_optional_fields_still_reads() -> None:
    body = dict(_compile(_request(readOnly=True)).to_json())
    for optional in ("baseVersion", "expiresInHours", "inheritedExpiry", "inheritedEnded"):
        body.pop(optional, None)

    computed = Computed.from_json(body)

    assert computed.base_version is None
    assert computed.expires_in_hours is None
    assert computed.inherited_expiry is None
    assert computed.inherited_ended is False


def test_a_stored_proposal_is_not_changed_by_finalizing_it() -> None:
    computed = _compile(_request(readOnly=True))
    before = computed.to_json()

    finalize(computed, computed.selected, [], "2026-10-05T12:00:00Z")

    assert computed.to_json() == before


# --- the request reader ------------------------------------------------------------------


def test_numbers_and_yes_or_no_are_accepted_as_allowed_values() -> None:
    limits = parse_limits(
        [{"operation": "*", "argument": "page", "allowedValues": [1, 2, True, "x"]}]
    )

    assert limits[0].values == (1, 2, True, "x")
    assert limits[0].regex() == "(?:1|2|true|x)"


@pytest.mark.parametrize("value", [1.5, None, [1], {"a": 1}])
def test_an_allowed_value_must_be_text_a_whole_number_or_yes_or_no(value: object) -> None:
    with pytest.raises(RequestValidationError, match="text, a whole number or true/false"):
        parse_limits([{"operation": "*", "argument": "page", "allowedValues": [value]}])


@pytest.mark.parametrize("notes", ["", "   ", "\x00\x01"])
def test_notes_that_say_nothing_are_left_out(notes: str) -> None:
    request = SetupRequest.parse(
        {"server": "learn", "userWords": "words", "notes": notes}, change=False
    )

    assert request.notes is None
    assert "notes" not in request.to_json()


def test_notes_that_say_something_are_kept_and_cut_clean() -> None:
    request = SetupRequest.parse(
        {"server": "learn", "userWords": "words", "notes": "  only on weekdays ‮ "}, change=False
    )

    assert request.notes == "only on weekdays"


# --- storage under races and damage ------------------------------------------------------


def test_a_proposal_has_ended_at_the_instant_and_not_before(tmp_path: Path) -> None:
    store, repository, connection = _store(tmp_path)
    with repository:
        proposal = store.get_proposal(_open(store, connection))

        assert isinstance(proposal, Proposal)
        assert proposal.expired(LATER) is True
        assert proposal.expired("2026-10-05T11:00:01Z") is True
        assert proposal.expired(NOW) is False
        assert proposal.expired("2026-10-05T10:59:59Z") is False


def test_a_proposal_that_moves_on_while_expiry_runs_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, repository, connection = _store(tmp_path)
    with repository:
        first = _open(store, connection, 1)
        second = _open(store, connection, 2)
        real = SetupStore.transition

        def lose_the_race(self: SetupStore, proposal_id: str, **fields: Any) -> Proposal:
            if proposal_id == first:
                raise ConflictError("someone else got there first")
            return real(self, proposal_id, **fields)

        monkeypatch.setattr(SetupStore, "transition", lose_the_race)

        ended = store.expire_due("2026-10-06T00:00:00Z")

        assert [item.proposal_id for item in ended] == [second]
        assert store.get_proposal(first).state == "waiting_for_owner"
        assert store.get_proposal(second).state == "expired"


@pytest.mark.parametrize(
    "stored",
    [
        "not json at all",
        '{"clientSecret": "s"}',
        '{"clientId": ""}',
        '{"clientId": 5}',
    ],
)
def test_app_details_that_cannot_be_read_are_treated_as_absent(tmp_path: Path, stored: str) -> None:
    store, repository, _ = _store(tmp_path)
    with repository:
        store.put_secret("OAUTH_APP:conn_0123456789ab", stored, NOW)

        assert store.oauth_client("conn_0123456789ab") is None
        assert store.oauth_client("conn_ffffffffffff") is None  # nothing stored at all


def test_app_details_are_read_back_with_or_without_a_secret(tmp_path: Path) -> None:
    store, repository, _ = _store(tmp_path)
    with repository:
        store.put_oauth_client("conn_0123456789ab", "client-id", "client-secret", NOW)
        store.put_oauth_client("conn_ba9876543210", "public-client", None, NOW)

        assert store.oauth_client("conn_0123456789ab") == ("client-id", "client-secret")
        assert store.oauth_client("conn_ba9876543210") == ("public-client", None)

        store.delete_oauth_client("conn_0123456789ab")
        assert store.oauth_client("conn_0123456789ab") is None


def test_the_database_holds_app_secrets_only_as_ciphertext(tmp_path: Path) -> None:
    store, repository, _ = _store(tmp_path)
    with repository:
        store.put_oauth_client("conn_0123456789ab", "client-id", "very-secret-value", NOW)

    with sqlite3.connect(tmp_path / "adapter.db") as raw:
        stored = " ".join(
            str(cell) for row in raw.execute("SELECT * FROM owner_secrets") for cell in row
        )
    assert "very-secret-value" not in stored
    assert "client-id" not in stored


def test_a_template_variable_with_no_name_takes_nothing() -> None:
    tool_index, tool_policy = _tool(PROJECT)
    index = {
        "tool:t": tool_index,
        "resource_template:r": {
            "type": "resource_template",
            "name": "r",
            "uriTemplate": "mcp://x/{+*}{?}{,}",
        },
    }

    assert not_held_to_limits({"tool:t": tool_policy}, index, set(index)) == []
