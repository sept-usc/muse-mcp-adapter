"""Turning a request into a policy: only what was asked, deterministic, and checked."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from muse_mcp_adapter.application.policy_template import name_suggests_read
from muse_mcp_adapter.chat_setup import directory
from muse_mcp_adapter.chat_setup.compile import (
    Computed,
    Exposure,
    classify_change,
    compile_proposal,
    finalize,
)
from muse_mcp_adapter.chat_setup.models import SetupRequest
from muse_mcp_adapter.domain.errors import RequestValidationError


def _tool(name: str, *, read_only: bool | None = None, destructive: bool = False) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": "tool",
        "name": name,
        "description": f"{name} does its thing.",
        "inputSchema": {"type": "object", "properties": {}},
    }
    annotations: dict[str, bool] = {}
    if read_only is not None:
        annotations["readOnlyHint"] = read_only
    if destructive:
        annotations["destructiveHint"] = True
    if annotations:
        item["annotations"] = annotations
    return item


PRIMITIVES: Sequence[Mapping[str, object]] = (
    _tool("list_issues", read_only=True),
    _tool("get_issue", read_only=True),
    _tool("create_issue"),
    _tool("remove_label", destructive=True),
    _tool("search_notes"),
    {"type": "resource", "name": "readme", "uri": "mcp://docs/readme", "mimeType": "text/plain"},
    {"type": "prompt", "name": "list_issues", "arguments": []},
)


def _request(**fields: object) -> SetupRequest:
    body: dict[str, object] = {"server": "learn", "userWords": "words"}
    body.update(fields)
    return SetupRequest.parse(body, change=False)


def _compile(request: SetupRequest, **kwargs: Any) -> Computed:
    defaults: dict[str, Any] = {
        "primitives": PRIMITIVES,
        "current": None,
        "current_digests": None,
        "snapshot_digest": "sha256:snap",
        "base_version": None,
    }
    defaults.update(kwargs)
    return compile_proposal(request=request, **defaults)


# --- the request ---------------------------------------------------------------------------


def test_a_request_is_parsed_strictly() -> None:
    request = SetupRequest.parse(
        {
            "server": "linear",
            "userWords": "  read\x00 only  ",
            "readOnly": True,
            "alsoAllow": ["create_issue", "create_issue", "tool:get_issue"],
            "timeoutSeconds": 20,
        },
        change=False,
    )
    assert request.user_words == "read  only"  # a control character becomes a space
    assert request.also_allow == ("create_issue", "tool:get_issue")
    assert SetupRequest.parse(request.to_json(), change=False) == request  # survives storage


@pytest.mark.parametrize(
    "body",
    [
        {"userWords": "x"},  # neither server nor endpoint
        {"userWords": "x", "server": "learn", "endpoint": "https://a.example/mcp"},
        {"server": "learn"},  # no words
        {"server": "learn", "userWords": "   "},
        {"server": "learn", "userWords": "x" * 2001},
        {"server": "Learn!", "userWords": "x"},
        {"server": "learn", "userWords": "x", "token": "secret"},
        {"server": "learn", "userWords": "x", "readOnly": "yes"},
        {"server": "learn", "userWords": "x", "alsoAllow": "create_issue"},
        {"server": "learn", "userWords": "x", "alsoAllow": ["bad name"]},
        {"server": "learn", "userWords": "x", "alsoAllow": [f"n{i}" for i in range(51)]},
        {"server": "learn", "userWords": "x", "timeoutSeconds": 0},
        {"server": "learn", "userWords": "x", "timeoutSeconds": True},
        {"server": "learn", "userWords": "x", "maxResultBytes": 10},
        {"server": "learn", "userWords": "x", "notes": 5},
        {"endpoint": "ftp://a.example/mcp", "userWords": "x"},
        {"endpoint": "https://a.example/mcp with space", "userWords": "x"},
    ],
)
def test_a_bad_request_is_refused(body: dict[str, object]) -> None:
    with pytest.raises(RequestValidationError):
        SetupRequest.parse(body, change=False)


def test_a_change_needs_a_connection_and_takes_no_address() -> None:
    with pytest.raises(RequestValidationError):
        SetupRequest.parse({"userWords": "x", "readOnly": True}, change=True)
    with pytest.raises(RequestValidationError):
        SetupRequest.parse(
            {"userWords": "x", "connectionId": "conn_0123456789ab", "server": "x"}, change=True
        )
    ok = SetupRequest.parse({"userWords": "x", "connectionId": "conn_0123456789ab"}, change=True)
    assert ok.connection_id == "conn_0123456789ab"


def test_the_list_of_services_is_searched_by_name_and_topic() -> None:
    assert [e.id for e in directory.search("linear")] == ["linear"]
    assert directory.search("issues")[0].id in {"github", "linear"}
    assert directory.search("!!!") == []
    assert directory.get("nope") is None
    assert "no sign-in" in directory.get("learn").sign_in_plan().lower()  # type: ignore[union-attr]
    assert "provider's own page" in directory.get("linear").sign_in_plan()  # type: ignore[union-attr]
    assert "token" in directory.get("github").sign_in_plan()  # type: ignore[union-attr]


def test_the_list_of_services_matches_the_ones_muse_vm_offers() -> None:
    from muse_vm import presets

    for entry in directory.ENTRIES:
        preset = presets.PRESETS[entry.id]
        assert (entry.endpoint, entry.credential) == (preset.endpoint, preset.credential)
    assert {e.id for e in directory.ENTRIES} == set(presets.PRESETS)


def test_names_that_read_like_lookups_are_recognised() -> None:
    assert name_suggests_read("search_notes") and name_suggests_read("getIssue")
    assert not name_suggests_read("delete_issue")
    assert not name_suggests_read("get_and_delete")
    assert not name_suggests_read("")


# --- compiling -----------------------------------------------------------------------------


def test_nothing_is_chosen_unless_the_request_names_it() -> None:
    computed = _compile(_request())
    assert computed.selected == ()
    assert all(
        item.classification in ("read", "write", "sensitive_write") for item in computed.candidates
    )
    assert all(not policy["allowed"] for policy in computed.policies.values())


def test_read_only_means_what_cannot_change_anything() -> None:
    computed = _compile(_request(readOnly=True))
    assert set(computed.selected) == {
        "tool:list_issues",
        "tool:get_issue",
        "resource:readme",
        "prompt:list_issues",
    }
    assert computed.requested == computed.selected


def test_named_things_are_chosen_and_unmatched_names_are_reported() -> None:
    computed = _compile(_request(alsoAllow=["create_issue", "nope", "tool:search_notes"]))
    assert set(computed.selected) == {"tool:create_issue", "tool:search_notes"}
    assert any(item.startswith("nope") for item in computed.unresolved)


def test_a_name_used_twice_is_not_guessed() -> None:
    computed = _compile(_request(alsoAllow=["list_issues"]))
    assert computed.selected == ()
    assert "more than one" in computed.unresolved[0]
    typed = _compile(_request(alsoAllow=["prompt:list_issues"]))
    assert typed.selected == ("prompt:list_issues",)


def test_exclusions_win_and_limits_apply_to_what_is_chosen() -> None:
    computed = _compile(
        _request(readOnly=True, exclude=["get_issue"], timeoutSeconds=7, maxResultBytes=4096)
    )
    assert "tool:get_issue" not in computed.selected
    policy = computed.policies["tool:list_issues"]
    assert policy["timeoutSeconds"] == 7 and policy["maxResultBytes"] == 4096
    assert computed.policies["tool:create_issue"]["allowed"] is False


def test_what_is_hinted_or_named_destructive_is_labelled_so() -> None:
    computed = _compile(_request(alsoAllow=["remove_label", "create_issue", "search_notes"]))
    by_key = {item.key: item for item in computed.candidates}
    assert by_key["tool:remove_label"].classification == "sensitive_write"
    assert by_key["tool:create_issue"].classification == "write"
    assert by_key["tool:search_notes"].reviewable and not by_key["tool:remove_label"].reviewable
    assert by_key["tool:list_issues"].evidence == "The server marks it read-only."
    assert "no hint" in by_key["tool:create_issue"].evidence
    assert (
        "destructive" in by_key["tool:remove_label"].evidence
        or "name" in by_key["tool:remove_label"].evidence
    )
    unselected = _compile(_request(readOnly=True))
    suggestions = [item.name for item in unselected.candidates if item.suggested]
    assert suggestions == ["search_notes"]  # looks like a lookup, but the server did not say so


def test_the_same_inputs_give_the_same_digest_and_any_change_alters_it() -> None:
    first = _compile(_request(readOnly=True))
    assert first.digest == _compile(_request(readOnly=True)).digest
    assert first.digest != _compile(_request(readOnly=True, alsoAllow=["create_issue"])).digest
    assert first.digest != _compile(_request(readOnly=True), snapshot_digest="sha256:other").digest
    assert first.digest != _compile(_request(readOnly=True, userWords="different")).digest


def test_a_compiled_proposal_survives_being_stored() -> None:
    computed = _compile(_request(readOnly=True, notes="only project X"))
    assert Computed.from_json(computed.to_json()) == computed
    assert computed.unenforced == ("Not enforced: only project X",)


def test_a_change_keeps_what_is_live_and_adds_only_what_is_asked() -> None:
    first = _compile(_request(readOnly=True))
    live = {key: dict(entry) for key, entry in first.policies.items() if entry["allowed"]}
    change = SetupRequest.parse(
        {"connectionId": "conn_0123456789ab", "userWords": "more", "alsoAllow": ["create_issue"]},
        change=True,
    )
    computed = compile_proposal(
        primitives=PRIMITIVES,
        request=change,
        current=live,
        current_digests=None,
        snapshot_digest="sha256:snap2",
        base_version="1.0.0",
    )
    assert set(computed.selected) == set(live) | {"tool:create_issue"}
    assert computed.requested == ("tool:create_issue",)
    assert computed.base_version == "1.0.0"


def test_a_definition_the_server_changed_is_drafted_afresh_under_the_same_name() -> None:
    from muse_mcp_adapter.catalog.canonical import digest

    first = _compile(_request(alsoAllow=["create_issue"]))
    live = {"tool:create_issue": dict(first.policies["tool:create_issue"])}
    live[next(iter(live))]["publicOperationId"] = "make_issue"
    change = SetupRequest.parse(
        {"connectionId": "conn_0123456789ab", "userWords": "x", "timeoutSeconds": 9}, change=True
    )
    computed = compile_proposal(
        primitives=PRIMITIVES,
        request=change,
        current=live,
        current_digests={"tool:create_issue": digest({"old": "definition"}), "tool:gone": "d"},
        snapshot_digest="sha256:s",
        base_version="1.0.0",
    )
    item = next(c for c in computed.candidates if c.key == "tool:create_issue")
    assert item.changed and item.public_operation_id == "make_issue"
    assert computed.gone == ()  # nothing is live that the server stopped offering here
    gone = compile_proposal(
        primitives=PRIMITIVES[:2],
        request=change,
        current=live,
        current_digests=None,
        snapshot_digest="sha256:s",
        base_version="1.0.0",
    )
    assert gone.gone == ("tool:create_issue",)


# --- what the owner finally chooses --------------------------------------------------------


def test_the_owner_can_remove_add_and_mark_read_only_but_only_from_what_is_offered() -> None:
    computed = _compile(_request(readOnly=True, alsoAllow=["create_issue"]))
    final = finalize(
        computed,
        chosen=["tool:list_issues", "tool:search_notes", "tool:create_issue"],
        reviewed_read=["tool:search_notes", "tool:list_issues", "tool:remove_label"],
    )
    assert final.added_by_owner == ("tool:search_notes",)
    assert "tool:get_issue" in final.removed_by_owner
    assert final.marked_read == ("tool:search_notes",)  # only a change-by-default tool, chosen
    assert final.exposed_not_requested == ("tool:search_notes",)
    by_key = {p["primitiveKey"]: p for p in final.policies}
    assert by_key["tool:search_notes"]["classification"] == "read"
    assert by_key["tool:search_notes"]["readOnlyReviewed"] is True
    assert by_key["tool:get_issue"]["allowed"] is False
    assert final.digest != finalize(computed, computed.selected, []).digest
    with pytest.raises(RequestValidationError):
        finalize(computed, ["tool:made_up"], [])


# --- widening and narrowing ----------------------------------------------------------------


def _exposure(digest: str = "d", **policy: object) -> Exposure:
    base: dict[str, object] = {
        "classification": "read",
        "timeoutSeconds": 30,
        "maxResultBytes": 1000,
        "description": "x",
    }
    base.update(policy)
    return Exposure(digest, base)


def test_a_change_is_narrowing_only_when_it_gives_nothing_new() -> None:
    old = {"a": _exposure(), "b": _exposure()}
    assert classify_change(old, dict(old)) == "none"
    assert classify_change(old, {"a": old["a"]}) == "narrowing"
    assert classify_change(old, {}) == "narrowing"
    assert classify_change({}, {}) == "none"
    assert classify_change(old, {**old, "c": _exposure()}) == "widening"
    assert classify_change(old, {"a": _exposure("other")}) == "widening"  # a new definition
    assert classify_change(old, {"a": _exposure(classification="write")}) == "narrowing"
    assert (
        classify_change({"a": _exposure(classification="write")}, {"a": _exposure()}) == "widening"
    )
    assert classify_change(old, {"a": _exposure(timeoutSeconds=10)}) == "narrowing"
    assert classify_change(old, {"a": _exposure(timeoutSeconds=60)}) == "widening"
    assert classify_change(old, {"a": _exposure(maxResultBytes=10)}) == "narrowing"
    assert classify_change(old, {"a": _exposure(maxResultBytes=5000)}) == "widening"
    assert classify_change(old, {"a": _exposure(description="changed")}) == "widening"
    assert classify_change(old, {"a": Exposure("d", {"classification": "read"})}) == "widening"
    assert classify_change(old, {"a": _exposure(classification="mystery")}) == "widening"
    unlimited = {"a": Exposure("d", {"classification": "read"})}
    tighter = {"a": Exposure("d", {"classification": "read", "timeoutSeconds": 5})}
    assert classify_change(unlimited, tighter) == "narrowing"
    assert classify_change(tighter, unlimited) == "widening"


def test_a_server_that_offers_too_much_cannot_be_reviewed_on_a_page() -> None:
    many = [_tool(f"tool_{number}") for number in range(2001)]
    with pytest.raises(RequestValidationError, match="too many to review"):
        _compile(_request(readOnly=True), primitives=many)
    _compile(_request(readOnly=True), primitives=many[:2000])
