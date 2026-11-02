"""Chat-driven setup over real sockets, with a service that signs in by OAuth.

A fake authorization server and a protected MCP server run locally, the adapter runs as the real
HTTP service, and two httpx clients play the agent and the owner's browser. Nothing is faked
inside the adapter: discovery, the sign-in redirects, the tokens and the calls are all real.
"""

from __future__ import annotations

from pathlib import Path

from tests.fixtures.chat_setup import connector_from, digest_of
from tests.fixtures.chat_world import world as _world


def test_a_service_that_signs_in_by_oauth_is_connected_from_one_sentence(tmp_path: Path) -> None:
    with _world(tmp_path) as w:
        proposal = w.ask(
            "propose_connection",
            {
                "endpoint": w.provider_endpoint,
                "userWords": "Connect the issue service and let it look issues up.",
                "alsoAllow": ["find_issue"],
            },
        )
        link = proposal["reviewLink"]

        # the owner opens the link on their own device and unlocks it
        w.browser.get(link)
        assert w.post(f"{link}/unlock", code=w.owner_code).status_code == 302
        first = w.browser.get(link)
        assert "not in the adapter's list" in first.text

        # signing in happens on the provider's own page; the browser comes back to the review
        started = w.post(f"{link}/start", plan="oauth")
        assert started.status_code == 302 and "/connect/" in started.headers["location"]
        back = w.browser.get(started.headers["location"], follow_redirects=True)
        assert back.status_code == 200 and "Look at what it offers" in back.text
        assert w.provider.authorizations, "the owner really signed in at the provider"
        assert w.ask("get_proposal_status", {"proposalId": proposal["proposalId"]})["state"] == (
            "connecting"
        )

        # then the adapter looks at the server with the owner's own sign-in
        assert w.post(f"{link}/discover").status_code == 302
        page = w.browser.get(link).text
        assert "<b>find_issue</b>" in page and "Changes data" in page  # no hint, so a change
        approved = w.post(
            f"{link}/decide",
            action="approve",
            digest=digest_of(page),
            chosen=["tool:find_issue"],
            read=["tool:find_issue"],  # the owner checked it only reads
        )
        assert approved.status_code == 302

        address, token = connector_from(w.browser.get(link).text)
        status = w.ask("get_proposal_status", {"proposalId": proposal["proposalId"]})
        assert status["state"] == "ready" and status["connector"]["apiDescriptionUrl"] == address

        # the call goes through with the owner's sign-in, which Muse never held
        called = w.agent.post(
            address.removesuffix("/openapi.json") + "/operations/find_issue",
            json={"issue_id": "ISSUE-7"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert called.status_code == 200, called.text
        assert called.json()["structuredContent"] == {"id": "ISSUE-7", "title": "Example"}
        assert w.repository.verify_admin_audit_chain().ok


def test_a_service_that_was_never_signed_in_cannot_be_looked_at(tmp_path: Path) -> None:
    with _world(tmp_path) as w:
        proposal = w.ask(
            "propose_connection",
            {"endpoint": w.provider_endpoint, "userWords": "connect it", "readOnly": True},
        )
        link = proposal["reviewLink"]
        w.browser.get(link)
        w.post(f"{link}/unlock", code=w.owner_code)
        w.browser.get(link)

        # the owner skips the sign-in and goes straight to looking: the page says what is missing
        w.post(f"{link}/start", plan="oauth")  # starts, but the browser is not sent on
        looked = w.post(f"{link}/discover")
        assert looked.status_code == 200 and "not signed in" in looked.text.lower()
        assert w.ask("get_proposal_status", {"proposalId": proposal["proposalId"]})["state"] == (
            "connecting"
        )
