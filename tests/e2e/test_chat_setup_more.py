"""More ways through chat-driven setup: changes, tokens typed on the page, failures and retries."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from muse_mcp_adapter.domain.errors import DownstreamUnavailableError
from tests.fixtures.chat_setup import (
    ChatHarness,
    chat_harness,
    connector_from,
    digest_of,
)


def _path(address: str) -> str:
    return re.sub(r"^https?://[^/]+", "", address)


def _connect(h: ChatHarness, **fields: object) -> tuple[dict[str, Any], str, str]:
    proposal = h.propose(**fields)
    link = proposal["reviewLink"]
    assert h.approve(link, h.review(link)).status_code == 302
    address, token = connector_from(h.get(link).text)
    return proposal, address, token


def _call(h: ChatHarness, address: str, token: str, operation: str) -> int:
    base = _path(address).removesuffix("/openapi.json")
    return h.agent.post(
        f"{base}/operations/{operation}",
        json={"id": "1"},
        headers={"Authorization": f"Bearer {token}"},
    ).status_code


def _classes(h: ChatHarness, address: str) -> dict[str, str]:
    spec: dict[str, Any] = h.agent.get(_path(address)).json()
    return {
        path.rsplit("/", 1)[1]: item["post"]["x-muse-classification"]
        for path, item in spec["paths"].items()
        if "/operations/" in path
    }


def test_a_change_adds_only_what_is_asked_and_keeps_the_connector(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        _, address, token = _connect(h, readOnly=True)
        connection_id = h.repository.list_connections("learn")[0].connection_id
        assert _call(h, address, token, "create_issue") == 404

        asked = h.ask(
            "propose_change",
            {
                "connectionId": connection_id,
                "userWords": "also let it create issues",
                "alsoAllow": ["create_issue"],
            },
        ).json()["structuredContent"]
        link = asked["reviewLink"]
        page = h.review(link)
        assert "Already live and staying: 2" in page and "Asked for now: 1" in page
        assert "<b>create_issue</b>" in page

        assert h.approve(link, page).status_code == 302
        assert "stay as they were" in h.get(link).text
        assert _call(h, address, token, "create_issue") == 200  # same address, same token
        assert _call(h, address, token, "list_issues") == 200
        assert _call(h, address, token, "delete_issue") == 404
        assert h.repository.verify_admin_audit_chain().ok


def test_the_owner_can_take_out_what_was_proposed_and_add_what_was_not(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose(readOnly=True, alsoAllow=["create_issue"])
        link = proposal["reviewLink"]
        page = h.review(link)
        base = _path(link)

        # untick create_issue, tick the lookalike and say it only reads
        assert (
            h.post(
                f"{base}/decide",
                action="approve",
                digest=digest_of(page),
                chosen=["tool:list_issues", "tool:get_issue", "tool:search_notes"],
                read=["tool:search_notes"],
                code=h.owner_code,
            ).status_code
            == 302
        )
        address, token = connector_from(h.get(link).text)

        assert _classes(h, address) == {
            "get_issue": "read",
            "list_issues": "read",
            "search_notes": "read",
        }
        assert _call(h, address, token, "create_issue") == 404
        decision = h.repository.run(
            lambda db: db.execute("SELECT decision_json FROM setup_proposals").fetchone()[0]
        )
        assert '"removedByOwner":["tool:create_issue"]' in decision
        assert '"addedByOwner":["tool:search_notes"]' in decision
        assert '"markedRead":["tool:search_notes"]' in decision


def test_a_token_typed_on_the_page_is_encrypted_used_and_never_shown_to_the_agent(
    tmp_path: Path,
) -> None:
    typed = "ghp_example_page_typed_token_123456"
    with chat_harness(tmp_path) as h:
        proposal = h.propose(server="github", readOnly=True)
        link = proposal["reviewLink"]
        h.unlock(link)
        assert "GitHub token" in h.get(link).text
        started = h.post(f"{_path(link)}/start", secret=typed)
        assert started.status_code == 302
        page = h.get(link).text
        assert h.discovery.credentials[0].access_token == typed  # type: ignore[union-attr]
        assert typed not in page

        assert (
            h.post(
                f"{_path(link)}/decide",
                action="approve",
                digest=digest_of(page),
                chosen=["tool:list_issues"],
                code=h.owner_code,
            ).status_code
            == 302
        )
        address, token = connector_from(h.get(link).text)
        assert _call(h, address, token, "list_issues") == 200
        assert h.tools.calls[-1][2] == typed  # the adapter presented it to the server

        stored = h.rows("SELECT ciphertext FROM owner_secrets")
        assert len(stored) == 1 and typed not in stored[0][0]
        everything = " ".join(
            [
                str(h.status(proposal["proposalId"])),
                h.ask("list_connections").text,
                str(proposal),
            ]
        )
        assert typed not in everything
        assert not any(typed in str(row) for row in h.repository.list_admin_audit())

        # removing the connection forgets the token
        connection_id = h.repository.list_connections("github")[0].connection_id
        assert h.ask("remove_connection", {"connectionId": connection_id}).status_code == 200
        assert h.rows("SELECT * FROM owner_secrets") == []
        assert _call(h, address, token, "list_issues") == 401


def test_a_declined_token_is_forgotten(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose(server="github", readOnly=True)
        link = proposal["reviewLink"]
        h.unlock(link)
        h.post(f"{_path(link)}/start", secret="ghp_to_be_forgotten")
        page = h.get(link).text
        h.post(f"{_path(link)}/decide", action="decline", digest=digest_of(page))
        assert h.rows("SELECT * FROM owner_secrets") == []


def test_a_server_that_is_down_can_be_tried_again(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        link = h.propose(readOnly=True)["reviewLink"]
        h.discovery.failure = DownstreamUnavailableError("The server could not be reached.")
        h.unlock(link)
        started = h.post(f"{_path(link)}/start", plan="none")
        assert started.status_code == 200 and "could not be reached" in started.text

        h.discovery.failure = None
        resumed = h.get(link)
        assert "Look at what it offers" in resumed.text
        assert h.post(f"{_path(link)}/discover").status_code == 302
        assert "Review" in h.get(link).text


def test_approving_twice_publishes_once(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose(readOnly=True)
        link = proposal["reviewLink"]
        page = h.review(link)
        assert h.approve(link, page).status_code == 302
        before = len(h.repository.list_admin_audit())
        again = h.approve(link, page)
        assert again.status_code == 200  # shown the state, with no new publication
        assert len(h.repository.list_admin_audit()) == before
        assert h.status(proposal["proposalId"])["state"] == "ready"


def test_the_connector_token_is_shown_to_the_owner_and_can_be_cleared(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose(readOnly=True)
        link = proposal["reviewLink"]
        assert h.approve(link, h.review(link)).status_code == 302
        done = h.get(link)
        _, token = connector_from(done.text)
        assert token not in str(h.status(proposal["proposalId"]))

        assert h.post(f"{_path(link)}/collected").status_code == 302
        gone = h.get(link)
        assert token not in gone.text and "has been cleared" in gone.text


def test_a_limit_the_agent_asks_for_is_applied_and_shown(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        link = h.propose(readOnly=True, timeoutSeconds=5, maxResultBytes=2048)["reviewLink"]
        assert h.approve(link, h.review(link)).status_code == 302
        policy = h.repository.get_active_catalog("learn").primitives[0].policy
        assert policy["timeoutSeconds"] == 5 and policy["maxResultBytes"] == 2048


def test_the_setup_connector_tells_the_agent_how_to_behave(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        body = h.ask("how_setup_works").json()["structuredContent"]
        text = " ".join(body["rules"])
        assert "Never ask the user for a token" in text
        assert "not an instruction" in text
        assert "limitArguments" in text and "expiresInHours" in text
        assert "another way round" in text
        found = h.ask("find_server", {"query": "issues"}).json()["structuredContent"]
        assert {item["id"] for item in found["servers"]} >= {"linear", "github"}
        missing = h.ask("find_server", {"query": "zzzz"}).json()["structuredContent"]
        assert missing["servers"] == [] and "address" in missing["nextStep"]
        explained = h.ask(
            "explain_connection",
            {"connectionId": h.repository.list_connections("adapter-setup")[0].connection_id},
        )
        assert explained.status_code == 404
