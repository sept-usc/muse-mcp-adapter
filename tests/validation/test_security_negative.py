# pyright: basic
"""Hostile input and hostile servers: the connector must stay safe and stay up."""

import json

import pytest

from tests.validation.env import AtlasHandle, ConnectorHandle

pytestmark = pytest.mark.validation
World = tuple[AtlasHandle, ConnectorHandle]


def test_instructions_in_results_come_back_as_data(world: World) -> None:
    atlas, conn = world
    response = conn.call("search_knowledge", {"query": "injection"})
    assert response.status_code == 200
    # Whatever the server says is carried as content; it never alters the connector itself.
    assert conn.http.get(f"/c/{conn.slug}/openapi.json").json() == conn.spec


def test_a_redirecting_server_is_not_followed(world: World) -> None:
    atlas, conn = world
    atlas.control("redirect", {"to": "http://127.0.0.1:1/elsewhere"})
    response = conn.call("get_issue", {"issue_id": "ISSUE-1"})
    assert response.status_code == 502
    atlas.control("redirect", {"to": None})
    assert conn.call("get_issue", {"issue_id": "ISSUE-1"}).status_code == 200


def test_secrets_are_not_in_the_spec_or_the_database(world: World) -> None:
    atlas, conn = world
    conn.call("create_issue", {"title": "x"}, **{"Idempotency-Key": "sec"})
    spec = json.dumps(conn.spec)
    assert atlas.options.bearer_token not in spec and conn.link.token not in spec
    raw = conn.database.read_bytes()
    assert conn.link.token.encode() not in raw  # only a hash of the token is stored


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "[1, 2, 3]",
        "null",
        '{"issue_id": ' + "[" * 5000 + "]" * 5000 + "}",
        '{"issue_id": 12}',
    ],
    ids=["not-json", "array", "null", "deeply-nested", "wrong-type"],
)
def test_malformed_bodies_are_client_errors_not_crashes(world: World, body: str) -> None:
    atlas, conn = world
    response = conn.http.post(
        f"/c/{conn.slug}/operations/get_issue",
        content=body,
        headers={**conn.headers, "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert conn.call("get_issue", {"issue_id": "ISSUE-1"}).status_code == 200  # still serving


@pytest.mark.parametrize(
    "path",
    [
        "/operations/../openapi.json",
        "/operations/get_issue%2f..%2fcreate_issue",
        "/operations/%00",
        "/operations/" + "a" * 5000,
        "/operations/__proto__",
        "/executions/' OR 1=1 --",
    ],
    ids=["dotdot", "encoded-slash", "nul", "very-long", "proto", "sql"],
)
def test_odd_paths_find_nothing(world: World, path: str) -> None:
    atlas, conn = world
    response = conn.http.post(f"/c/{conn.slug}{path}", json={}, headers=conn.headers)
    assert response.status_code in (400, 404, 405)
    assert atlas.effects() == []


def test_wrong_methods_are_refused(world: World) -> None:
    atlas, conn = world
    assert (
        conn.http.get(f"/c/{conn.slug}/operations/get_issue", headers=conn.headers).status_code
        == 405
    )
    assert conn.http.delete(f"/c/{conn.slug}/openapi.json", headers=conn.headers).status_code == 405


def test_an_oversized_body_is_refused(world: World) -> None:
    atlas, conn = world
    huge = {"issue_id": "x" * 3_000_000}
    assert conn.call("get_issue", huge).status_code in (400, 413)


def test_an_unlisted_tool_cannot_be_called_by_name(world: World) -> None:
    atlas, conn = world
    assert conn.call("reset_everything").status_code == 404
    assert conn.call("get_operation_status").status_code in (404, 400, 405)
