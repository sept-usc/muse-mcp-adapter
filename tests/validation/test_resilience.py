# pyright: basic
"""The connector under failure: servers going away, slow, or unable to do what MCP assumes."""

import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.live_server import free_port
from tests.validation.env import AtlasHandle, ConnectorHandle, atlas_server, connector

pytestmark = pytest.mark.validation
BODY = {"title": "Resilience"}


def _timeout(tool: str, seconds: int):  # type: ignore[no-untyped-def]
    def adjust(policies: list[dict[str, Any]]) -> None:
        for policy in policies:
            if policy["primitiveKey"] == f"tool:{tool}":
                policy["timeoutSeconds"] = seconds

    return adjust


def test_a_server_that_goes_away_and_returns_is_retried_safely(tmp_path: Path) -> None:
    port = free_port()
    stack = ExitStack()
    atlas = stack.enter_context(atlas_server(port=port))
    with connector(tmp_path, atlas) as conn:
        assert conn.call("get_issue", {"issue_id": "ISSUE-1"}).status_code == 200

        stack.close()  # the MCP server stops
        down = conn.call("create_issue", BODY, **{"Idempotency-Key": "k"})
        assert down.status_code == 502
        operation = (
            conn.status(down.json().get("operationId", ""))
            if "operationId" in down.json()
            else None
        )
        assert operation is None or operation.status_code in (200, 404)
        row = conn.repository.run(
            lambda db: db.execute(
                "SELECT status, retryable FROM operations WHERE public_operation_id='create_issue'"
            ).fetchone()
        )
        assert (row["status"], row["retryable"]) == ("FAILED", 1)  # nothing was sent: safe to retry
        assert conn.call("get_issue", {"issue_id": "ISSUE-1"}).status_code == 502

        with atlas_server(port=port) as returned:  # the same address comes back
            retried = conn.call("create_issue", BODY, **{"Idempotency-Key": "k"})
            assert retried.status_code == 200
            assert len(returned.effects("create_issue")) == 1


def test_a_write_that_times_out_is_unknown_and_really_did_happen(tmp_path: Path) -> None:
    with (
        atlas_server() as atlas,
        connector(tmp_path, atlas, adjust=_timeout("create_issue", 1)) as conn,
    ):
        atlas.control("faults", {"create_issue": {"delay_seconds": 2.5}})
        late = conn.call("create_issue", BODY, **{"Idempotency-Key": "slow"})
        assert late.status_code == 409 and late.json()["error"] == "UNKNOWN_OUTCOME"

        # Whether the server finished is not knowable from outside, which is why this is UNKNOWN
        # and not FAILED.
        time.sleep(2.5)
        again = conn.call("create_issue", BODY, **{"Idempotency-Key": "slow"})
        assert again.status_code == 409  # still blocked until the owner settles it


def test_a_read_that_times_out_fails_and_can_be_retried(tmp_path: Path) -> None:
    with atlas_server() as atlas, connector(tmp_path, atlas, adjust=_timeout("slow", 1)) as conn:
        started = time.monotonic()
        late = conn.call("slow", {"seconds": 4})
        assert late.status_code == 502
        assert time.monotonic() - started < 3  # gave up at its own limit, not the server's pace
        assert conn.call("slow", {"seconds": 0}).status_code == 200


def test_the_per_server_concurrency_limit_protects_the_server(tmp_path: Path) -> None:
    with (
        atlas_server() as atlas,
        connector(tmp_path, atlas, live=True, per_server_concurrency=2) as conn,
    ):

        def call(_: int) -> int:
            return conn.call("slow", {"seconds": 1}).status_code

        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(call, range(6)))
        assert statuses.count(200) >= 2
        assert 429 in statuses  # excess requests are refused, not queued without bound
        assert set(statuses) <= {200, 429}


def test_the_connector_keeps_serving_after_a_failed_call(
    world: tuple[AtlasHandle, ConnectorHandle],
) -> None:
    atlas, conn = world
    atlas.control("faults", {"get_issue": {"fail_first": 3}})
    results = [conn.call("get_issue", {"issue_id": "ISSUE-1"}).status_code for _ in range(5)]
    assert results == [502, 502, 502, 200, 200]  # no lingering damage from the failures


def test_a_stateless_server_works_for_everything_that_does_not_ask_questions(
    tmp_path: Path,
) -> None:
    with (
        atlas_server(stateless=True) as atlas,
        connector(tmp_path, atlas, adjust=_timeout("schedule_review", 2)) as conn,
    ):
        assert conn.call("get_issue", {"issue_id": "ISSUE-1"}).status_code == 200
        assert conn.call("create_issue", BODY, **{"Idempotency-Key": "s"}).status_code == 200
        assert conn.call("handbook").status_code == 200
        assert conn.call("triage", {"issue_id": "ISSUE-1"}).status_code == 200
        assert len(atlas.effects("create_issue")) == 1

        # MCP itself cannot route a question back to the caller on a stateless server
        asked = conn.call("schedule_review", {"issue_id": "ISSUE-1"})
        assert asked.status_code in (409, 502)
        assert atlas.effects("schedule_review") == []
