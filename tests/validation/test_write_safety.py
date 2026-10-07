# pyright: basic
"""Writes happen exactly once, or the connector says it does not know.

Atlas records every real side effect in a ledger. That ledger is the ground truth: a test
compares what the connector reported with what really happened.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from muse_mcp_adapter.cli import main as cli
from tests.validation.env import AtlasHandle, ConnectorHandle, atlas_server, connector

pytestmark = pytest.mark.validation

WORLD = tuple[AtlasHandle, ConnectorHandle]
BODY = {"title": "Dark mode"}


def _key(value: str) -> dict[str, str]:
    return {"Idempotency-Key": value}


def test_a_retried_write_with_the_same_key_runs_once(world: WORLD) -> None:
    atlas, conn = world
    responses = [conn.call("create_issue", BODY, **_key("retry-1")) for _ in range(5)]
    assert {r.status_code for r in responses} == {200}
    assert len({r.json()["operationId"] for r in responses}) == 1
    assert len(atlas.effects("create_issue")) == 1


def test_different_keys_are_different_actions(world: WORLD) -> None:
    atlas, conn = world
    conn.call("create_issue", BODY, **_key("a"))
    conn.call("create_issue", BODY, **_key("b"))
    assert len(atlas.effects("create_issue")) == 2


def test_without_a_key_identical_requests_close_together_are_one_action(world: WORLD) -> None:
    atlas, conn = world
    first = conn.call("create_issue", BODY)
    second = conn.call("create_issue", BODY)
    assert first.json()["operationId"] == second.json()["operationId"]
    assert len(atlas.effects("create_issue")) == 1


def test_the_same_key_with_a_different_request_is_refused(world: WORLD) -> None:
    atlas, conn = world
    conn.call("create_issue", BODY, **_key("k"))
    clash = conn.call("create_issue", {"title": "Something else"}, **_key("k"))
    assert clash.status_code == 409 and clash.json()["error"] == "IDEMPOTENCY_CONFLICT"
    assert len(atlas.effects("create_issue")) == 1


def test_a_retry_storm_runs_each_write_once(world: WORLD) -> None:
    atlas, conn = world
    for _ in range(20):
        conn.call("send_notification", {"recipient": "a@example.com", "message": "hi"}, **_key("n"))
    assert len(atlas.effects("send_notification")) == 1


def test_concurrent_identical_requests_never_duplicate(tmp_path: Path) -> None:
    with atlas_server() as atlas, connector(tmp_path, atlas, live=True) as conn:

        def send(_: int) -> int:
            return conn.call("create_issue", BODY, **_key("race")).status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(send, range(16)))
        assert set(statuses) <= {200, 409}  # done, or told it is still in progress
        assert 200 in statuses
        assert len(atlas.effects("create_issue")) == 1


def test_a_connection_lost_after_the_write_is_unknown_and_blocks_retries(world: WORLD) -> None:
    atlas, conn = world
    atlas.control("faults", {"create_issue": {"drop_after_effect": True}})

    lost = conn.call("create_issue", BODY, **_key("lost"))
    assert lost.status_code == 409 and lost.json()["error"] == "UNKNOWN_OUTCOME"
    assert len(atlas.effects("create_issue")) == 1  # the write did happen

    again = conn.call("create_issue", BODY, **_key("lost"))
    assert again.status_code == 409 and again.json()["error"] == "UNKNOWN_OUTCOME"
    assert len(atlas.effects("create_issue")) == 1  # and was not repeated

    operation_id = lost.json()["operationId"]
    assert conn.status(operation_id).json()["status"] == "UNKNOWN"


def test_the_owner_settles_an_unknown_write_after_checking_the_system(
    world: WORLD, tmp_path: Path
) -> None:
    atlas, conn = world
    atlas.control("faults", {"create_issue": {"drop_after_effect": True}})
    lost = conn.call("create_issue", BODY, **_key("settle"))
    operation_id = lost.json()["operationId"]

    effect = atlas.effects("create_issue")[0]["detail"]  # the owner looks at the real system
    result = {
        "requestId": "recovered",
        "catalogVersion": "1.0.0",
        "publicOperationId": "create_issue",
        "primitiveType": "tool",
        "provenance": {},
        "operationId": operation_id,
        "operationStatus": "COMPLETED",
        "structuredContent": {"id": effect["id"], "title": effect["title"], "body": "",
                              "status": "open", "labels": []},
        "contents": [],
        "messages": [],
    }  # fmt: skip
    response_file = tmp_path / "response.json"
    response_file.write_text(json.dumps(result))
    code = cli(["--database", str(conn.database), "--actor", "owner", "reconcile",
                "--operation-id", operation_id, "--outcome", "completed",
                "--response", str(response_file)])  # fmt: skip
    assert code == 0

    settled = conn.call("create_issue", BODY, **_key("settle"))
    assert settled.status_code == 200
    assert settled.json()["structuredContent"]["id"] == effect["id"]
    assert len(atlas.effects("create_issue")) == 1  # settling never repeats the write


def test_a_connection_lost_before_the_write_can_be_retried_once_confirmed(
    world: WORLD,
) -> None:
    atlas, conn = world
    atlas.control("faults", {"create_issue": {"drop_before_effect": True}})
    lost = conn.call("create_issue", BODY, **_key("before"))
    assert lost.status_code == 409 and lost.json()["error"] == "UNKNOWN_OUTCOME"
    assert atlas.effects("create_issue") == []  # nothing happened, but the adapter cannot know

    code = cli(["--database", str(conn.database), "--actor", "owner", "reconcile",
                "--operation-id", lost.json()["operationId"], "--outcome", "not_applied"])  # fmt: skip  # noqa: E501
    assert code == 0
    retried = conn.call("create_issue", BODY, **_key("before"))
    assert retried.status_code == 200
    assert len(atlas.effects("create_issue")) == 1


def test_a_write_the_server_rejects_is_final_for_its_key_and_leaves_no_effect(
    world: WORLD,
) -> None:
    atlas, conn = world
    atlas.control("faults", {"create_issue": {"fail_first": 1, "error_message": "Disk full"}})

    failed = conn.call("create_issue", BODY, **_key("rejected"))
    assert failed.status_code == 502 and "Disk full" in failed.json()["message"]
    assert atlas.effects("create_issue") == []

    calls = atlas.state.calls["create_issue"]
    repeated = conn.call("create_issue", BODY, **_key("rejected"))
    assert repeated.status_code == 502
    assert atlas.state.calls["create_issue"] == calls  # the stored failure is returned, no new call

    fresh = conn.call("create_issue", BODY, **_key("rejected-2"))  # a new key is a new attempt
    assert fresh.status_code == 200
    assert len(atlas.effects("create_issue")) == 1


def test_reads_are_never_deduplicated_and_transient_failures_can_be_retried(
    world: WORLD,
) -> None:
    atlas, conn = world
    outcomes = [conn.call("flaky", {"key": "k", "failures": 2}).status_code for _ in range(3)]
    assert outcomes == [502, 502, 200]
    assert atlas.state.calls["flaky"] == 3


def test_deletes_and_updates_are_exactly_once_too(world: WORLD) -> None:
    atlas, conn = world
    for _ in range(3):
        conn.call("delete_issue", {"issue_id": "ISSUE-3"}, **_key("del"))
        conn.call("update_issue", {"issue_id": "ISSUE-1", "status": "closed"}, **_key("upd"))
    assert len(atlas.effects("delete_issue")) == 1
    assert len(atlas.effects("update_issue")) == 1


def test_a_write_the_caller_never_retries_after_a_dropped_response_is_still_visible_as_unknown(
    world: WORLD,
) -> None:
    atlas, conn = world
    atlas.control("faults", {"add_comment": {"drop_after_effect": True}})
    lost = conn.call("add_comment", {"issue_id": "ISSUE-1", "text": "hello"})
    assert lost.json()["error"] == "UNKNOWN_OUTCOME"
    unknown = conn.repository.run(
        lambda db: db.execute(
            "SELECT COUNT(*) AS n FROM operations WHERE status='UNKNOWN'"
        ).fetchone()
    )
    assert unknown["n"] == 1
    assert len(atlas.effects("add_comment")) == 1


def test_an_identical_request_without_a_key_is_flagged_as_a_replay(world) -> None:  # type: ignore[no-untyped-def]
    atlas, conn = world
    first = conn.call("create_issue", BODY)
    again = conn.call("create_issue", BODY)
    assert first.status_code == again.status_code == 200
    assert "idempotentReplay" not in first.json()
    assert again.json()["idempotentReplay"] is True
    assert "Idempotency-Key" in again.json()["note"]
    assert len(atlas.effects("create_issue")) == 1  # nothing new ran

    deliberate = conn.call("create_issue", BODY, **{"Idempotency-Key": "second-copy"})
    assert "idempotentReplay" not in deliberate.json()
    assert len(atlas.effects("create_issue")) == 2  # a fresh key really runs it again
