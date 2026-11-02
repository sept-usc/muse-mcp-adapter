"""A service that only signs in through an app the owner registered with it (Slack, Asana, ...).

Such a provider has no dynamic registration, so the owner registers an app once and types its
client ID and secret on the review page. The adapter signs in as that app, over real sockets, and
the secret never reaches Muse, a page it fetches, the audit trail or the database in the clear.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from tests.fixtures.chat_setup import chat_harness, connector_from, digest_of
from tests.fixtures.chat_world import World
from tests.fixtures.chat_world import world as _world

APP_ID = "owner-app-1234.5678"
APP_SECRET = "s3cr3t-value-of-the-owners-app"


def _unlocked(w: World, link: str) -> None:
    w.browser.get(link)
    assert w.post(f"{link}/unlock", code=w.owner_code).status_code == 302
    assert w.browser.get(link).status_code == 200


def _everything_the_world_can_read(w: World, *pages: str) -> str:
    """Every place a secret could leak to: pages, the audit trail and the database file."""
    audit = " ".join(str(row) for row in w.repository.list_admin_audit())
    database = w.database.read_bytes().decode("latin-1")
    return " ".join([*pages, audit, database])


def test_the_owners_own_app_is_used_to_sign_in_and_its_secret_stays_private(
    tmp_path: Path,
) -> None:
    with _world(tmp_path, registration=False) as w:
        w.provider.register_app(APP_ID, APP_SECRET, f"{w.adapter}/oauth/callback")
        proposal = w.ask(
            "propose_connection",
            {
                "endpoint": w.provider_endpoint,
                "userWords": "Connect the issue service and let it look issues up.",
                "alsoAllow": ["find_issue"],
            },
        )
        link = proposal["reviewLink"]
        _unlocked(w, link)

        # the owner says it needs an app, and the page says what to enter where
        first = w.browser.get(link).text
        assert "with an app I registered with the service" in first
        assert f"{w.adapter}/oauth/callback" in first

        started = w.post(
            f"{link}/start", plan="oauth_app", client_id=APP_ID, client_secret=APP_SECRET
        )
        assert started.status_code == 302 and "/connect/" in started.headers["location"]
        back = w.browser.get(started.headers["location"], follow_redirects=True)
        assert back.status_code == 200 and "Look at what it offers" in back.text
        assert w.provider.authorized_clients == [APP_ID]
        assert w.provider.token_requests == 1  # the code was exchanged with the app's secret

        assert w.post(f"{link}/discover").status_code == 302
        page = w.browser.get(link).text
        approved = w.post(
            f"{link}/decide",
            action="approve",
            digest=digest_of(page),
            chosen=["tool:find_issue"],
            read=["tool:find_issue"],
        )
        assert approved.status_code == 302
        done = w.browser.get(link).text
        address, token = connector_from(done)

        called = w.agent.post(
            address.removesuffix("/openapi.json") + "/operations/find_issue",
            json={"issue_id": "ISSUE-9"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert called.status_code == 200, called.text

        # what Muse is told never holds the app, and the secret is nowhere in the clear
        status = str(w.ask("get_proposal_status", {"proposalId": proposal["proposalId"]}))
        listed = str(w.ask("list_connections", {}))
        readable = _everything_the_world_can_read(w, first, page, done, status, listed)
        assert APP_SECRET not in readable and APP_ID not in readable.replace(
            w.provider_endpoint, ""
        )
        assert w.repository.verify_admin_audit_chain().ok
        connecting = [
            row
            for row in w.repository.list_admin_audit()
            if str(row["action"]).endswith("connecting")
        ]
        assert cast(Mapping[str, object], connecting[0]["data"])["ownApp"] is True


def test_a_provider_that_cannot_register_apps_needs_the_owners_app(tmp_path: Path) -> None:
    with _world(tmp_path, registration=False) as w:
        proposal = w.ask(
            "propose_connection",
            {"endpoint": w.provider_endpoint, "userWords": "connect it", "readOnly": True},
        )
        link = proposal["reviewLink"]
        _unlocked(w, link)

        # signing in as a program that registers itself is not possible here
        started = w.post(f"{link}/start", plan="oauth")
        assert started.status_code == 302
        refused = w.browser.get(started.headers["location"], follow_redirects=True)
        assert refused.status_code == 502 and "did not offer a sign-in" in refused.text
        assert w.provider.authorizations == []


def test_a_wrong_client_secret_connects_nothing_and_cancelling_forgets_the_app(
    tmp_path: Path,
) -> None:
    with _world(tmp_path, registration=False) as w:
        w.provider.register_app(APP_ID, APP_SECRET, f"{w.adapter}/oauth/callback")
        proposal = w.ask(
            "propose_connection",
            {"endpoint": w.provider_endpoint, "userWords": "connect it", "readOnly": True},
        )
        link = proposal["reviewLink"]
        _unlocked(w, link)
        started = w.post(
            f"{link}/start", plan="oauth_app", client_id=APP_ID, client_secret="not-the-secret"
        )
        failed = w.browser.get(started.headers["location"], follow_redirects=True)
        assert failed.status_code == 400 and "Not connected" in failed.text
        state = w.ask("get_proposal_status", {"proposalId": proposal["proposalId"]})["state"]
        assert state == "connecting"
        assert w.rows("SELECT name FROM owner_secrets WHERE name LIKE 'OAUTH_APP:%'")

        # the page offers a way out; taking it forgets the app and what was started
        assert "Cancel this request" in w.browser.get(link).text
        assert w.post(f"{link}/cancel").status_code == 302
        assert w.ask("get_proposal_status", {"proposalId": proposal["proposalId"]})["state"] == (
            "declined"
        )
        assert not w.rows("SELECT name FROM owner_secrets")
        assert w.browser.get(link).status_code == 410
        assert w.repository.get_server(w.repository.list_servers()[-1].server_id).enabled is False


def test_new_details_replace_the_ones_a_failed_sign_in_used(tmp_path: Path) -> None:
    with _world(tmp_path, registration=False) as w:
        w.provider.register_app(APP_ID, APP_SECRET, f"{w.adapter}/oauth/callback")
        asked = {"endpoint": w.provider_endpoint, "userWords": "connect it", "readOnly": True}

        first = w.ask("propose_connection", {**asked, "userWords": "first try"})
        _unlocked(w, first["reviewLink"])
        wrong = w.post(
            f"{first['reviewLink']}/start",
            plan="oauth_app",
            client_id=APP_ID,
            client_secret="typo",
        )
        w.browser.get(wrong.headers["location"], follow_redirects=True)
        assert w.post(f"{first['reviewLink']}/cancel").status_code == 302

        second = w.ask("propose_connection", {**asked, "userWords": "second try"})
        link = second["reviewLink"]
        _unlocked(w, link)
        started = w.post(
            f"{link}/start", plan="oauth_app", client_id=APP_ID, client_secret=APP_SECRET
        )
        back = w.browser.get(started.headers["location"], follow_redirects=True)
        assert back.status_code == 200 and "Look at what it offers" in back.text
        assert w.ask("get_proposal_status", {"proposalId": second["proposalId"]})["state"] == (
            "connecting"
        )


def test_the_app_details_are_checked_before_anything_is_created(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.ask("propose_connection", {"server": "slack", "userWords": "connect slack"})
        link = proposal.json()["structuredContent"]["reviewLink"]
        h.unlock(link)

        # a listed service that needs an app says so, and where to register it
        page = h.get(link).text
        assert "only signs in through an app that you register" in page
        assert "https://adapter.example.com/oauth/callback" in page
        assert "https://docs.slack.dev/ai/mcp-server" in page
        assert "name=client_id" in page and "name=client_secret" in page

        for fields in (
            {},  # nothing typed
            {"client_id": "has a space"},
            {"client_id": "x" * 501},
            {"client_id": "ok-id", "client_secret": "bad\x01secret"},
        ):
            answered = h.post(f"{link}/start", **fields)
            assert answered.status_code == 200, fields
            assert re.search(r"client ID|client secret", answered.text), fields
        assert [s.server_id for s in h.repository.list_servers()] == ["adapter-setup"]
        assert not h.rows("SELECT name FROM owner_secrets")

        # with valid details a draft exists and the browser is sent to sign in
        accepted = h.post(f"{link}/start", client_id="123.456", client_secret="shh")
        assert accepted.status_code == 302 and "/connect/" in accepted.headers["location"]
        stored = h.rows("SELECT name, ciphertext FROM owner_secrets")
        assert len(stored) == 1 and stored[0][0].startswith("OAUTH_APP:conn_")
        assert "123.456" not in stored[0][1] and "shh" not in stored[0][1]  # sealed

        connection = h.repository.list_connections("slack")[0].connection_id
        assert stored[0][0] == f"OAUTH_APP:{connection}"

        # cancelling from the page forgets the app and leaves nothing registered or enabled
        assert h.post(f"{link}/cancel").status_code == 302
        assert not h.rows("SELECT name FROM owner_secrets")
        assert h.repository.get_server("slack").enabled is False
        assert h.get(link).status_code == 410
