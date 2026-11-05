"""Small things that would confuse an owner or an agent if they went wrong."""

from __future__ import annotations

import re
from pathlib import Path

from tests.fixtures.chat_setup import chat_harness, connector_from


def _path(link: str) -> str:
    return re.sub(r"^https?://[^/]+", "", link)


def test_a_repeated_request_reports_where_the_first_one_has_got_to(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        first = h.propose(readOnly=True)
        h.review(first["reviewLink"])  # the owner has started and is now reviewing
        again = h.propose(readOnly=True)
        assert again["proposalId"] == first["proposalId"] and again["state"] == "in_review"
        assert h.get(first["reviewLink"]).status_code == 404  # the old link stopped working


def test_a_connection_still_being_set_up_is_not_listed_as_one(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose(readOnly=True)
        h.review(proposal["reviewLink"])  # the draft exists, nothing is approved
        listed = h.ask("list_connections").json()["structuredContent"]
        assert listed["connections"] == []
        assert [item["state"] for item in listed["proposalsWaiting"]] == ["in_review"]


def test_an_applied_change_is_not_described_as_a_new_connector(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        first = h.propose(readOnly=True)
        h.approve(first["reviewLink"], h.review(first["reviewLink"]))
        connector_from(h.get(first["reviewLink"]).text)
        connection_id = h.repository.list_connections("learn")[0].connection_id
        change = h.ask(
            "propose_change",
            {
                "connectionId": connection_id,
                "userWords": "add create",
                "alsoAllow": ["create_issue"],
            },
        ).json()["structuredContent"]
        h.approve(change["reviewLink"], h.review(change["reviewLink"]))
        status = h.status(change["proposalId"])
        assert status["state"] == "ready" and "unchanged" in status["nextStep"]
        assert "Create a connector" not in status["nextStep"]
        assert {item["id"] for item in status["connector"]["operations"]} >= {"create_issue"}


def test_pressing_continue_twice_is_not_an_error(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        link = h.propose(readOnly=True)["reviewLink"]
        h.unlock(link)
        h.get(link)
        assert h.post(f"{_path(link)}/start", plan="none").status_code == 302
        second = h.post(f"{_path(link)}/start", plan="none")
        assert second.status_code in (200, 302)
        assert "not ready" not in second.text
