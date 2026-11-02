"""Access the owner gave for a time: how the end is read, compared, validated and shown."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from openapi_spec_validator import validate

from muse_mcp_adapter.application.openapi import compile_openapi
from muse_mcp_adapter.chat_setup.compile import (
    Computed,
    Exposure,
    classify_change,
    compile_proposal,
    finalize,
)
from muse_mcp_adapter.chat_setup.models import SetupRequest
from muse_mcp_adapter.control_plane.compile_catalog import compile_catalog
from muse_mcp_adapter.policy import expiry
from muse_mcp_adapter.policy.validate_policy import validate_exposure_policy

NOON = "2026-10-05T12:00:00Z"


def test_an_end_is_a_utc_instant_and_anything_else_is_unreadable() -> None:
    assert expiry.parse(NOON) is not None
    assert expiry.parse("2026-10-05T12:00:00.250Z") is not None
    for unreadable in (None, 5, "", "2026-10-05T12:00:00", "2026-10-05T12:00:00+02:00", "soon Z"):
        assert expiry.parse(unreadable) is None
    assert expiry.parse("2026-10-05T12:00:00Z") == expiry.parse("2026-10-05T12:00:00.000Z")
    assert expiry.ends_at({"expiresAt": NOON}) == NOON
    assert expiry.ends_at({}) is None and expiry.ends_at({"expiresAt": 5}) is None


def test_access_has_ended_at_the_instant_and_a_damaged_end_fails_closed() -> None:
    policy = {"expiresAt": NOON}
    assert expiry.has_ended({}, NOON) is False  # no end at all
    assert expiry.has_ended(policy, "2026-10-05T11:59:59Z") is False
    assert expiry.has_ended(policy, NOON) is True
    assert expiry.has_ended(policy, "2026-10-06T00:00:00Z") is True
    assert expiry.has_ended({"expiresAt": "garbage"}, "2026-10-05T00:00:00Z") is True
    assert expiry.has_ended({"expiresAt": 7}, "2026-10-05T00:00:00Z") is True
    assert expiry.has_ended(policy, "not a time") is True  # cannot tell, so it has ended


def test_a_new_end_is_never_later_than_the_old_one() -> None:
    later = "2026-10-05T13:00:00Z"
    assert expiry.no_later(None, None) and expiry.no_later(None, NOON)  # giving an end narrows
    assert expiry.no_later(NOON, NOON) and expiry.no_later(later, NOON)
    assert not expiry.no_later(NOON, later)
    assert not expiry.no_later(NOON, None)  # taking the end away widens
    assert not expiry.no_later("garbage", NOON) and not expiry.no_later(NOON, "garbage")


def _tool_policy(**extra: object) -> tuple[dict[str, object], dict[str, object]]:
    primitive: dict[str, object] = {
        "type": "tool",
        "name": "find_issue",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
    }
    policy: dict[str, object] = {
        "primitiveKey": "tool:find_issue",
        "allowed": True,
        "publicOperationId": "find_issue",
        "classification": "read",
        "readOnlyReviewed": True,
        "maxResultBytes": 1000,
        **extra,
    }
    return primitive, policy


def test_a_published_end_must_be_a_readable_utc_time() -> None:
    assert validate_exposure_policy(*_tool_policy(expiresAt=NOON)) == []
    assert validate_exposure_policy(*_tool_policy()) == []
    for bad in ("tomorrow", "2026-10-05T12:00:00", 5, ""):
        issues = validate_exposure_policy(*_tool_policy(expiresAt=bad))
        assert any("expiresAt" in issue for issue in issues), bad


def _catalog(*ends: str | None) -> Any:
    primitives: list[dict[str, object]] = []
    policies: list[dict[str, object]] = []
    for number, end in enumerate(ends):
        name = f"tool_{number}"
        primitive, policy = _tool_policy(**({"expiresAt": end} if end else {}))
        primitive.update(name=name)
        policy.update(primitiveKey=f"tool:{name}", publicOperationId=name)
        primitives.append(primitive)
        policies.append(policy)
    return compile_catalog(
        catalog_version="1.0.0",
        server={"id": "project-system", "endpoint": "https://mcp.example.test/mcp"},
        protocol_version="2025-11-25",
        discovered_at=NOON,
        published_at=NOON,
        primitives=primitives,
        policies=policies,
    )


def test_the_spec_leaves_out_what_has_ended_and_says_when_the_rest_ends() -> None:
    catalog = _catalog(NOON, "2026-10-05T18:00:00Z", None)

    everything: Any = compile_openapi(catalog, public_url="https://a.example.com", slug="abc")
    assert {"/operations/tool_0", "/operations/tool_1", "/operations/tool_2"} <= set(
        everything["paths"]
    )  # no clock given, so nothing is hidden

    before: Any = compile_openapi(
        catalog, public_url="https://a.example.com", slug="abc", now="2026-10-05T11:00:00Z"
    )
    validate(dict(before))
    assert "/operations/tool_0" in before["paths"]
    described = before["paths"]["/operations/tool_1"]["post"]["description"]
    assert "The owner set this access to end at 2026-10-05T18:00:00Z." in described
    assert "end at" not in before["paths"]["/operations/tool_2"]["post"]["description"]

    after: Any = compile_openapi(
        catalog, public_url="https://a.example.com", slug="abc", now="2026-10-05T12:00:00Z"
    )
    validate(dict(after))
    operations = {path for path in after["paths"] if path.startswith("/operations/")}
    assert operations == {"/operations/tool_1", "/operations/tool_2"}

    last: Any = compile_openapi(
        catalog, public_url="https://a.example.com", slug="abc", now="2026-10-06T00:00:00Z"
    )
    assert {path for path in last["paths"] if path.startswith("/operations/")} == {
        "/operations/tool_2"
    }


# --- what the owner approves ---------------------------------------------------------------


def _tool(name: str) -> dict[str, Any]:
    return {
        "type": "tool",
        "name": name,
        "description": f"{name}.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    }


def _compile(
    request: dict[str, object], live: Mapping[str, Mapping[str, object]] | None = None
) -> Computed:
    return compile_proposal(
        primitives=[_tool("a_tool"), _tool("b_tool")],
        request=SetupRequest.parse({"server": "learn", "userWords": "x", **request}, change=False),
        current=live,
        current_digests=None,
        snapshot_digest="sha256:s",
        base_version=None,
    )


@pytest.mark.parametrize("hours", [0, -1, 721, 1.5, "2", True])
def test_a_time_limit_must_be_a_whole_number_of_hours_up_to_thirty_days(hours: object) -> None:
    from muse_mcp_adapter.domain.errors import RequestValidationError

    with pytest.raises(RequestValidationError, match="expiresInHours"):
        SetupRequest.parse(
            {"server": "learn", "userWords": "x", "expiresInHours": hours}, change=False
        )
    request = SetupRequest.parse(
        {"server": "learn", "userWords": "x", "expiresInHours": 720}, change=False
    )
    assert request.expires_in_hours == 720
    assert SetupRequest.parse(request.to_json(), change=False) == request


def test_the_end_is_stamped_on_what_the_owner_chooses_when_they_approve() -> None:
    computed = _compile({"readOnly": True, "expiresInHours": 2})
    assert computed.expires_in_hours == 2 and computed.inherited_expiry is None
    assert all("expiresAt" not in policy for policy in computed.policies.values())  # not yet

    final = finalize(computed, ["tool:a_tool"], [], "2026-10-05T14:00:00Z")
    by_key = {str(policy["primitiveKey"]): policy for policy in final.policies}
    assert by_key["tool:a_tool"]["expiresAt"] == "2026-10-05T14:00:00Z"
    assert "expiresAt" not in by_key["tool:b_tool"]  # not exposed, so no end is written
    other = finalize(computed, ["tool:a_tool"], [], "2026-10-05T15:00:00Z")
    assert other.digest != final.digest  # the approval covers the end that was set
    assert finalize(computed, ["tool:a_tool"], []).digest != final.digest


def test_a_request_without_a_time_keeps_the_end_a_connection_already_has() -> None:
    live = {
        "tool:a_tool": {
            "primitiveKey": "tool:a_tool",
            "publicOperationId": "a_tool",
            "classification": "read",
            "readOnlyReviewed": True,
            "maxResultBytes": 1000,
            "allowed": True,
            "expiresAt": "2026-10-05T18:00:00Z",
        },
        "tool:other": {"expiresAt": "2026-10-05T13:00:00Z"},  # the soonest wins
    }
    computed = _compile({"alsoAllow": ["b_tool"]}, live)
    assert computed.inherited_expiry == "2026-10-05T13:00:00Z"

    final = finalize(computed, ["tool:a_tool", "tool:b_tool"], [])
    by_key = {str(policy["primitiveKey"]): policy for policy in final.policies}
    assert by_key["tool:a_tool"]["expiresAt"] == "2026-10-05T18:00:00Z"  # its own end stays
    assert by_key["tool:b_tool"]["expiresAt"] == "2026-10-05T13:00:00Z"  # the new one inherits

    asked = _compile({"alsoAllow": ["b_tool"], "expiresInHours": 4}, live)
    assert asked.inherited_expiry is None  # a new time replaces it, if the owner approves
    renewed = finalize(asked, ["tool:a_tool", "tool:b_tool"], [], "2026-10-05T20:00:00Z")
    assert {p["expiresAt"] for p in renewed.policies if p["allowed"]} == {"2026-10-05T20:00:00Z"}


def test_a_stored_proposal_from_before_time_limits_still_reads_back() -> None:
    computed = _compile({"readOnly": True, "expiresInHours": 3})
    stored = computed.to_json()
    assert Computed.from_json(stored).expires_in_hours == 3
    del stored["expiresInHours"], stored["inheritedExpiry"]
    old = Computed.from_json(stored)
    assert old.expires_in_hours is None and old.inherited_expiry is None


def test_ending_access_sooner_is_narrowing_and_ending_it_later_is_not() -> None:
    def exposure(end: str | None) -> dict[str, Exposure]:
        extra = {"expiresAt": end} if end else {}
        return {"a": Exposure("d", {"classification": "read", **extra})}

    soon, later = "2026-10-05T12:00:00Z", "2026-10-05T13:00:00Z"
    assert classify_change(exposure(None), exposure(soon)) == "narrowing"  # gave it an end
    assert classify_change(exposure(later), exposure(soon)) == "narrowing"  # shortened
    assert classify_change(exposure(soon), exposure(soon)) == "none"
    assert classify_change(exposure(soon), exposure(later)) == "widening"  # extended
    assert classify_change(exposure(soon), exposure(None)) == "widening"  # made permanent
    assert classify_change(exposure(soon), exposure("garbage")) == "widening"
