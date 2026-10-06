# pyright: basic
"""Upstream changes never alter what Muse sees until the owner reviews and republishes."""

import copy
from typing import Any

import pytest

from tests.validation.env import AtlasHandle, ConnectorHandle, drift, publish, rediscover, rollback

pytestmark = pytest.mark.validation
World = tuple[AtlasHandle, ConnectorHandle]


def _upstream_changes(atlas: AtlasHandle, conn: ConnectorHandle) -> set[tuple[str, str, str]]:
    atlas.control("profile", {"profile": "v2"})
    rediscover(conn)
    return drift(conn)


def test_every_kind_of_upstream_change_is_detected(world: World) -> None:
    atlas, conn = world
    assert drift(conn) == set()  # nothing has changed yet

    events = _upstream_changes(atlas, conn)
    assert ("tool:get_issue", "block", "input_schema_changed") in events  # a new required input
    assert (
        "tool:list_issues",
        "block",
        "annotations_changed",
    ) in events  # read-only became writable
    assert ("tool:search_knowledge", "block", "definition_changed") in events  # new description
    assert ("tool:echo_unicode", "block", "primitive_removed") in events
    assert ("tool:archive_issue", "review", "primitive_added") in events


def test_what_muse_sees_does_not_change_until_the_owner_publishes(world: World) -> None:
    atlas, conn = world
    before = conn.http.get(f"/c/{conn.slug}/openapi.json").json()
    _upstream_changes(atlas, conn)
    after = conn.http.get(f"/c/{conn.slug}/openapi.json").json()
    assert after == before  # the connector contract is stable

    assert "archive_issue" not in conn.operations()  # new upstream tools are not exposed
    assert conn.call("archive_issue", {"issue_id": "ISSUE-1"}).status_code == 404


def test_a_changed_description_cannot_reach_muse_while_it_is_pinned(world: World) -> None:
    atlas, conn = world
    original = conn.operations()["search_knowledge"]["description"]
    _upstream_changes(atlas, conn)

    # The owner republishes the reviewed policy (descriptions pinned), dropping the removed tool.
    reviewed = [
        copy.deepcopy(policy)
        for policy in conn.policies
        if policy["primitiveKey"] != "tool:echo_unicode"
    ]
    for policy in reviewed:
        if policy["primitiveKey"] == "tool:list_issues":
            policy["readOnlyReviewed"] = True  # the owner looked at the new annotations
    publish(conn, "2.0.0", reviewed)
    spec = conn.http.get(f"/c/{conn.slug}/openapi.json").json()
    republished = spec["paths"]["/operations/search_knowledge"]["post"]["description"]
    assert republished == original
    assert "full email" not in str(spec)  # the injected instruction never appears


def test_republishing_the_new_server_text_without_review_would_expose_it(world: World) -> None:
    """The risk that pinning exists to prevent, shown so it stays understood."""
    atlas, conn = world
    _upstream_changes(atlas, conn)
    from muse_mcp_adapter.application.policy_template import draft_policies

    fresh = draft_policies(rediscover(conn), expose_reads=True)
    for policy in fresh:
        policy["allowed"] = True
    publish(conn, "2.0.0", fresh)
    spec = conn.http.get(f"/c/{conn.slug}/openapi.json").json()
    assert "full email" in str(spec)  # a blind republish carries the injected text through


def test_a_new_version_reflects_schema_changes_and_rollback_restores_the_old_contract(
    world: World,
) -> None:
    atlas, conn = world
    v1 = conn.http.get(f"/c/{conn.slug}/openapi.json").json()
    _upstream_changes(atlas, conn)
    from muse_mcp_adapter.application.policy_template import draft_policies

    fresh = draft_policies(rediscover(conn), expose_reads=True)
    for policy in fresh:
        policy["allowed"] = True
    publish(conn, "2.0.0", fresh)

    spec = conn.http.get(f"/c/{conn.slug}/openapi.json").json()
    schema: Any = spec["paths"]["/operations/get_issue"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert "include_comments" in schema["required"]
    assert "archive_issue" in {p.rsplit("/", 1)[-1] for p in spec["paths"]}

    # the stricter schema is enforced by the connector itself
    assert conn.call("get_issue", {"issue_id": "ISSUE-1"}).status_code == 400
    assert (
        conn.call("get_issue", {"issue_id": "ISSUE-1", "include_comments": True}).status_code == 200
    )

    rollback(conn)
    assert conn.http.get(f"/c/{conn.slug}/openapi.json").json() == v1


def test_calls_to_a_removed_or_changed_tool_fail_cleanly_not_silently(world: World) -> None:
    atlas, conn = world
    _upstream_changes(atlas, conn)
    removed = conn.call("echo_unicode", {"text": "hi"})  # still published, gone upstream
    assert removed.status_code == 502 and removed.json()["error"] == "DOWNSTREAM_TOOL_ERROR"
    changed = conn.call("get_issue", {"issue_id": "ISSUE-1"})  # the server now wants more input
    assert changed.status_code == 502
    assert atlas.effects() == []
