"""Turning a request into a policy: only what was asked, deterministic, and checked."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

import pytest

from muse_mcp_adapter.application.policy_template import name_suggests_read
from muse_mcp_adapter.chat_setup import directory
from muse_mcp_adapter.chat_setup.compile import (
    Computed,
    compile_proposal,
    finalize,
)
from muse_mcp_adapter.chat_setup.models import SetupRequest
from muse_mcp_adapter.domain.errors import RequestValidationError
from muse_mcp_adapter.policy.argument_limits import values_pattern
from muse_mcp_adapter.policy.narrowing import Exposure, classify_change


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

    for entry in directory.PRESETS:  # the hand-written ones; the rest are generated
        preset = presets.PRESETS[entry.id]
        assert (entry.endpoint, entry.credential) == (preset.endpoint, preset.credential)
    assert {e.id for e in directory.PRESETS} == set(presets.PRESETS)


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


def test_what_was_already_live_is_not_counted_as_exposed_unasked_by_a_change() -> None:
    first = _compile(_request(readOnly=True))
    live = {key: dict(entry) for key, entry in first.policies.items() if entry["allowed"]}
    renewal = SetupRequest.parse(
        {"connectionId": "conn_0123456789ab", "userWords": "two more hours", "expiresInHours": 2},
        change=True,
    )
    computed = compile_proposal(
        primitives=PRIMITIVES,
        request=renewal,
        current=live,
        current_digests=None,
        snapshot_digest="sha256:snap3",
        base_version="1.0.0",
    )
    final = finalize(computed, computed.selected, [])

    assert computed.requested == ()
    assert final.exposed_not_requested == ()  # nothing new: it was all live already

    wider = finalize(computed, [*computed.selected, "tool:search_notes"], [])
    assert wider.exposed_not_requested == ("tool:search_notes",)  # what the owner added is named


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


# --- limits on what an operation may be called with -----------------------------------------


def _with_arguments(name: str, *arguments: str, read_only: bool | None = True) -> dict[str, Any]:
    item = _tool(name, read_only=read_only)
    item["inputSchema"] = {
        "type": "object",
        "properties": {a: {"type": "string"} for a in arguments},
    }
    return item


def _limit(operation: object = "*", argument: str = "project", **kind: object) -> dict[str, object]:
    return {"operation": operation, "argument": argument, **({"allowedValues": ["ENG"]} | kind)}


@pytest.mark.parametrize(
    "limits, message",
    [
        ("project", "must be a list"),
        ([_limit()] * 21, "at most 20"),
        (["project"], "not a limit"),
        ([_limit(secret="x")], "does not accept: secret"),
        ([_limit(operation="not valid")], "name of an operation"),
        ([_limit(operation=5)], "name of an operation"),
        ([_limit(argument="not valid")], "name of an argument"),
        ([{"operation": "*", "argument": "project"}], "either allowedValues or a pattern"),
        ([_limit(pattern="ENG")], "either allowedValues or a pattern"),
        ([{"operation": "*", "argument": "p", "pattern": ""}], "short text"),
        ([{"operation": "*", "argument": "p", "pattern": "x" * 201}], "short text"),
        ([{"operation": "*", "argument": "p", "pattern": 5}], "short text"),
        ([{"operation": "*", "argument": "p", "pattern": "(a+)+$"}], "backtracking"),
        ([_limit(allowedValues="ENG")], "must be a list"),
        ([_limit(allowedValues=[])], "1 to 20 values"),
        ([_limit(allowedValues=[""])], "short, non-empty"),
        ([_limit(allowedValues=["x" * 201])], "short, non-empty"),
        ([_limit(allowedValues=[1.5])], "text, a whole number or true/false"),
        ([_limit(allowedValues=[["ENG"]])], "text, a whole number or true/false"),
    ],
)
def test_a_limit_is_checked_strictly(limits: object, message: str) -> None:
    with pytest.raises(RequestValidationError, match=message):
        _request(readOnly=True, limitArguments=limits)


def test_limits_survive_being_stored_and_change_the_digest() -> None:
    request = _request(readOnly=True, limitArguments=[_limit()])
    assert SetupRequest.parse(request.to_json(), change=False) == request
    pattern = _request(
        readOnly=True, limitArguments=[{"operation": "*", "argument": "project", "pattern": "E.*"}]
    )
    assert SetupRequest.parse(pattern.to_json(), change=False) == pattern
    primitives = [_with_arguments("list_issues", "project")]
    digests = {
        _compile(r, primitives=primitives).digest
        for r in (_request(readOnly=True), request, pattern)
    }
    assert len(digests) == 3


def test_a_limit_goes_on_every_tool_that_takes_the_argument_chosen_or_not() -> None:
    primitives = [
        _with_arguments("list_issues", "project"),
        _with_arguments("create_issue", "project", read_only=None),
        _with_arguments("search_notes", "query"),
    ]
    computed = _compile(_request(readOnly=True, limitArguments=[_limit()]), primitives=primitives)

    assert computed.policies["tool:list_issues"]["argumentPatterns"] == {"project": "(?:ENG)"}
    # create_issue is not chosen, but the owner may tick it on the page and then it is limited
    assert computed.policies["tool:create_issue"]["argumentPatterns"] == {"project": "(?:ENG)"}
    assert computed.policies["tool:create_issue"]["allowed"] is False
    assert "argumentPatterns" not in computed.policies["tool:search_notes"]
    assert computed.unresolved == ()
    shown = {item.name: item.limits for item in computed.candidates}
    assert shown["list_issues"] == (("project", "ENG"),)
    assert shown["search_notes"] == ()


def test_a_limit_that_matches_nothing_is_reported_and_limits_per_tool_are_capped() -> None:
    names = [f"a{number}" for number in range(11)]
    primitives = [_with_arguments("list_issues", "project", *names)]
    limits = [_limit(argument=name) for name in names] + [_limit(argument="project")]
    computed = _compile(_request(readOnly=True, limitArguments=limits), primitives=primitives)
    assert any("too many limits on one operation" in note for note in computed.unresolved)
    assert (
        len(cast(Mapping[str, str], computed.policies["tool:list_issues"]["argumentPatterns"]))
        == 10
    )

    nothing = _compile(
        _request(
            readOnly=True,
            limitArguments=[
                _limit(operation="readme"),  # a resource is not a tool
                _limit(operation="list_issues", argument="colour"),
                _limit(argument="colour"),
            ],
        ),
        primitives=[*PRIMITIVES, _with_arguments("list_issues2", "project")],
    )
    text = " | ".join(nothing.unresolved)
    assert "the server does not offer that operation" in text
    assert "the operation has no such argument" in text
    assert "no operation has that argument" in text


def test_an_operation_can_be_named_by_the_id_muse_knows() -> None:
    first = _compile(
        _request(readOnly=True, limitArguments=[_limit(operation="list_issues")]),
        primitives=[_with_arguments("list_issues", "project")],
    )
    assert "argumentPatterns" in first.policies["tool:list_issues"]
    live = {"tool:list_issues": first.policies["tool:list_issues"]}
    renamed = {**live["tool:list_issues"], "publicOperationId": "issues_list"}
    second = _compile(
        _request(readOnly=True, limitArguments=[_limit(operation="issues_list")]),
        primitives=[_with_arguments("list_issues", "project")],
        current={"tool:list_issues": renamed},
    )
    assert second.unresolved == ()


def test_unlimited_arguments_are_listed_and_the_list_is_capped() -> None:
    primitives = [_with_arguments("only_this", "project")] + [
        _with_arguments(f"other_{number:02d}", "project") for number in range(12)
    ]
    computed = _compile(
        _request(readOnly=True, limitArguments=[_limit(operation="only_this")]),
        primitives=primitives,
    )
    assert len(computed.unenforced) == 11
    assert computed.unenforced[0] == (
        "other_00 takes project, which is limited on only_this but is not limited on other_00."
    )
    assert computed.unenforced[-1] == "...and 2 more like these."


def test_a_limit_is_narrowing_to_add_or_shrink_and_widening_to_remove_or_grow() -> None:
    both = values_pattern(["ENG", "OPS"])
    eng = values_pattern(["ENG"])

    def with_limit(pattern: str | None) -> dict[str, Exposure]:
        extra = {} if pattern is None else {"argumentPatterns": {"project": pattern}}
        return {"a": Exposure("d", {"classification": "read", **extra})}

    assert classify_change(with_limit(None), with_limit(eng)) == "narrowing"  # added
    assert classify_change(with_limit(both), with_limit(eng)) == "narrowing"  # fewer values
    assert classify_change(with_limit(both), with_limit(both)) == "none"
    assert classify_change(with_limit(eng), with_limit(both)) == "widening"  # more values
    assert classify_change(with_limit(eng), with_limit(None)) == "widening"  # removed
    assert classify_change(with_limit(eng), with_limit("[A-Z]+")) == "widening"  # not provable
    corrupt = {"a": Exposure("d", {"classification": "read", "argumentPatterns": ["project"]})}
    assert classify_change(corrupt, with_limit(eng)) == "widening"
    assert classify_change(with_limit(eng), corrupt) == "widening"


def test_limits_are_kept_when_the_server_redefines_an_operation_unless_the_argument_went() -> None:
    old = _compile(
        _request(readOnly=True, limitArguments=[_limit()]),
        primitives=[_with_arguments("list_issues", "project", "query")],
    )
    live = {key: dict(entry) for key, entry in old.policies.items() if entry["allowed"]}
    stale = {"tool:list_issues": "sha256:old"}

    kept = _compile(
        SetupRequest.parse({"connectionId": "conn_000000000001", "userWords": "x"}, change=True),
        primitives=[_with_arguments("list_issues", "project", "query", "more")],
        current=live,
        current_digests=stale,
    )
    assert kept.policies["tool:list_issues"]["argumentPatterns"] == {"project": "(?:ENG)"}
    assert kept.unenforced == ()

    gone = _compile(
        SetupRequest.parse({"connectionId": "conn_000000000001", "userWords": "x"}, change=True),
        primitives=[_with_arguments("list_issues", "query")],
        current=live,
        current_digests=stale,
    )
    assert "argumentPatterns" not in gone.policies["tool:list_issues"]
    assert gone.unenforced == (
        "list_issues: the limit on project (ENG) is gone because the server's new definition "
        "has no such argument.",
    )
