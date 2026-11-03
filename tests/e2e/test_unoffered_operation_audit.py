"""A caller that proves who it is and asks for something that is not offered leaves a trace."""

from __future__ import annotations

from pathlib import Path

from tests.fixtures.chat_setup import chat_harness


def test_a_call_for_an_operation_that_is_not_offered_is_kept_in_the_audit(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        before = len(h.repository.list_runtime_audit())

        for name in ("approve_proposal", "merge_pull_request", "x" * 300):
            assert h.ask(name).status_code == 404

        rows = h.repository.list_runtime_audit()[before:]
        assert [row["outcome"] for row in rows] == ["failed"] * 3
        assert [row["error_code"] for row in rows] == ["EntityNotFoundError"] * 3
        assert [row["public_operation_id"] for row in rows[:2]] == [
            "approve_proposal",
            "merge_pull_request",
        ]
        assert len(str(rows[2]["public_operation_id"])) == 120  # a long name is kept bounded
        assert all(row["connection_id"] and row["classification"] is None for row in rows)


def test_a_caller_without_a_valid_token_cannot_be_attributed_so_is_not_recorded(
    tmp_path: Path,
) -> None:
    with chat_harness(tmp_path) as h:
        before = len(h.repository.list_runtime_audit())

        response = h.ask("approve_proposal", token="mcpa_" + "A" * 43)

        assert response.status_code == 401
        assert len(h.repository.list_runtime_audit()) == before
