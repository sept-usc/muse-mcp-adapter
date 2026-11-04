"""Chat setup, scenario by scenario: what an owner and an agent each see from start to finish.

Each test is one situation a real person gets into: a session that runs out, a page opened twice,
a connection removed and asked for again, two services side by side, a restart in the middle.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from muse_mcp_adapter.application.serve import build_application, load_serve_config
from muse_mcp_adapter.domain.errors import EntityNotFoundError
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from tests.fixtures.chat_setup import (
    CSRF_COOKIE,
    ChatHarness,
    chat_harness,
    connector_from,
    projects_manifest,
    run_cli,
)
from tests.fixtures.service_harness import PUBLIC_URL

CSRF_FIELD = re.compile(r"name=csrf value='([^']+)'")
CODE_PROMPT = "Enter your owner code"


def _path(link_or_path: str) -> str:
    return re.sub(r"^https?://[^/]+", "", link_or_path)


def _text(html: str) -> str:
    return " ".join(
        re.sub(r"<[^>]+>", " ", re.sub(r"<style.*?</style>", "", html, flags=re.S)).split()
    )


def _connect(h: ChatHarness, **fields: object) -> tuple[dict[str, Any], str, str, str]:
    proposal = h.propose(**fields)
    link = proposal["reviewLink"]
    assert h.approve(link, h.review(link)).status_code == 302
    address, token = connector_from(h.get(link).text)
    return proposal, link, address, token


def _call(h: ChatHarness, address: str, token: str, operation: str, **arguments: object) -> Any:
    base = _path(address).removesuffix("/openapi.json")
    return h.agent.post(
        f"{base}/operations/{operation}",
        json=arguments,
        headers={"Authorization": f"Bearer {token}"},
    )


def _connection(h: ChatHarness, server: str = "learn") -> str:
    return h.repository.list_connections(server)[0].connection_id


def _state(h: ChatHarness, proposal_id: str) -> str:
    return str(h.status(proposal_id)["state"])


def _set_state(h: ChatHarness, proposal_id: str, state: str) -> None:
    with sqlite3.connect(h.database) as raw:
        raw.execute(
            "UPDATE setup_proposals SET state = ? WHERE proposal_id = ?", (state, proposal_id)
        )


# --- the owner's session and forms -------------------------------------------------------


def test_the_owner_is_asked_for_the_code_again_when_the_session_runs_out(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose()
        link = proposal["reviewLink"]
        assert h.unlock(link).status_code == 302

        h.clock.advance(29 * 60)
        assert CODE_PROMPT not in h.get(link).text  # still inside the half hour

        h.clock.advance(2 * 60)
        assert CODE_PROMPT in h.get(link).text
        pressed = h.post(f"{_path(link)}/start")  # a form from before the end does nothing
        assert pressed.status_code == 302
        assert _state(h, proposal["proposalId"]) == "waiting_for_owner"

        assert h.unlock(link).status_code == 302  # the code gets the owner back in
        assert CODE_PROMPT not in h.get(link).text


def test_the_cross_site_token_is_kept_across_pages_and_replaced_when_damaged(
    tmp_path: Path,
) -> None:
    with chat_harness(tmp_path) as h:
        link = h.propose()["reviewLink"]
        first = CSRF_FIELD.search(h.get(link).text)
        assert first is not None
        issued = h.browser.cookies.get(CSRF_COOKIE)
        assert issued is not None and issued == first.group(1) and len(issued) >= 24

        assert h.unlock(link).status_code == 302
        for _ in range(3):  # every later page shows the same token the browser holds
            shown = CSRF_FIELD.search(h.get(link).text)
            assert shown is not None and shown.group(1) == issued
        assert h.browser.cookies.get(CSRF_COOKIE) == issued

        for cookie in h.browser.cookies.jar:
            if cookie.name == CSRF_COOKIE:
                cookie.value = "short"  # damaged, or planted by someone else
        replaced = CSRF_FIELD.search(h.get(link).text)
        assert replaced is not None and replaced.group(1) != "short"
        assert h.browser.cookies.get(CSRF_COOKIE) == replaced.group(1)


def test_a_form_larger_than_a_page_of_choices_is_refused_however_it_is_sent(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose()
        path = _path(proposal["reviewLink"])
        h.get(path)
        big = b"code=" + b"x" * (128 * 1024 + 1)
        headers = {"content-type": "application/x-www-form-urlencoded"}

        declared = h.browser.post(f"{path}/unlock", content=big, headers=headers)
        streamed = h.browser.post(
            f"{path}/unlock",
            content=(big[index : index + 8192] for index in range(0, len(big), 8192)),
            headers=headers,
        )

        assert declared.status_code == 413 and streamed.status_code == 413
        assert "too large" in _text(declared.text)
        assert _state(h, proposal["proposalId"]) == "waiting_for_owner"


# --- the page for each state a proposal can be in ----------------------------------------


def test_a_proposal_being_put_in_place_says_so_and_does_nothing_else(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose()
        link = proposal["reviewLink"]
        assert h.unlock(link).status_code == 302
        _set_state(h, proposal["proposalId"], "approved")  # between the approval and the result

        page = h.get(link)

        assert page.status_code == 200
        assert "being put in place" in _text(page.text)
        assert "<form" not in page.text
        assert "being put in place" in h.status(proposal["proposalId"])["message"]


def test_a_service_that_signs_in_on_its_own_page_says_so_before_anything_is_contacted(
    tmp_path: Path,
) -> None:
    with chat_harness(tmp_path) as h:
        link = h.propose(server="linear")["reviewLink"]
        assert h.unlock(link).status_code == 302

        page = h.get(link)

        text = _text(page.text)
        assert "You will sign in on mcp.linear.app" in text
        assert "own page. The adapter keeps the result; Muse never sees it." in text
        assert "Sign in and continue" in page.text
        assert h.discovery.credentials == []  # nothing has been looked at yet


def test_a_request_can_be_cancelled_until_there_is_something_to_review(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        waiting = h.propose(userWords="first")
        reviewing = h.propose(userWords="second")
        assert h.unlock(waiting["reviewLink"]).status_code == 302

        cancelled = h.post(f"{_path(waiting['reviewLink'])}/cancel")
        assert cancelled.status_code == 302
        assert _state(h, waiting["proposalId"]) == "declined"
        assert "declined" in h.status(waiting["proposalId"])["message"]
        assert h.rows("SELECT COUNT(*) FROM connections WHERE revoked_at IS NULL")[0][0] == 1

        h.review(reviewing["reviewLink"])  # now it is in review: too late to cancel
        late = h.post(f"{_path(reviewing['reviewLink'])}/cancel")
        assert "not waiting to be connected" in _text(late.text)
        assert _state(h, reviewing["proposalId"]) == "in_review"


def test_looking_at_the_server_again_after_it_was_down_needs_no_new_request(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose()
        link = proposal["reviewLink"]
        path = _path(link)
        assert h.unlock(link).status_code == 302
        too_soon = h.post(f"{path}/discover")  # nothing has been started yet
        assert "not ready to look at the server" in _text(too_soon.text)

        h.discovery.failure = RuntimeError("the server is down")
        failed = h.post(f"{path}/start")
        assert "the server is down" in _text(failed.text)
        assert _state(h, proposal["proposalId"]) == "connecting"

        h.discovery.failure = None
        assert h.post(f"{path}/discover").status_code == 302
        assert _state(h, proposal["proposalId"]) == "in_review"
        assert "What would become available" in h.get(link).text


def test_collecting_before_there_is_a_token_does_nothing(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose()
        link = proposal["reviewLink"]
        page = h.review(link)

        assert h.post(f"{_path(link)}/collected").status_code == 302

        assert _state(h, proposal["proposalId"]) == "in_review"
        assert h.approve(link, page).status_code == 302  # and the token is still to come
        assert connector_from(h.get(link).text)[1].startswith("mcpa_")


def test_the_agent_is_told_what_each_stage_means_as_the_owner_moves_through_them(
    tmp_path: Path,
) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose()
        link, pid = proposal["reviewLink"], proposal["proposalId"]
        asked = h.status(pid)
        assert asked["state"] == "waiting_for_owner"
        assert "Do not claim it is connected" in asked["nextStep"]

        h.discovery.failure = RuntimeError("down")
        h.unlock(link)
        h.post(f"{_path(link)}/start")
        started = h.status(pid)
        assert (
            started["state"] == "connecting"
            and "signing in or entering a token" in started["message"]
        )

        h.discovery.failure = None
        h.post(f"{_path(link)}/discover")
        reviewing = h.status(pid)
        assert reviewing["state"] == "in_review" and "reviewing" in reviewing["message"]

        page = h.get(link).text
        assert h.approve(link, page).status_code == 302
        ready = h.status(pid)
        assert ready["state"] == "ready"
        assert ready["connector"]["apiDescriptionUrl"].endswith("/openapi.json")
        assert "Do not ask for it in the chat" in ready["nextStep"]


def test_a_proposal_nobody_answers_expires_and_the_agent_is_told_to_ask_again(
    tmp_path: Path,
) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose()

        h.clock.advance(25 * 3600)

        status = h.status(proposal["proposalId"])
        assert status["state"] == "expired"
        assert "Propose again" in status["message"]
        page = h.get(proposal["reviewLink"])
        assert "expired" in _text(page.text).lower()


# --- a connection's whole life -----------------------------------------------------------


def test_a_connection_that_was_removed_can_be_asked_for_again_with_a_new_token(
    tmp_path: Path,
) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        _, _, address, token = _connect(h, readOnly=True)
        connection = _connection(h)
        assert _call(h, address, token, "list_issues").status_code == 200

        gone = h.ask("remove_connection", {"connectionId": connection})
        assert (
            gone.status_code == 200
            and "disconnected" in gone.json()["structuredContent"]["message"]
        )
        assert _call(h, address, token, "list_issues").status_code == 401  # the token is dead
        assert h.ask("list_connections").json()["structuredContent"]["connections"] == []

        again, _, address2, token2 = _connect(h, readOnly=True, userWords="connect it again")

        assert address2 != address and token2 != token
        assert _call(h, address2, token2, "list_issues").status_code == 200
        assert _call(h, address, token, "list_issues").status_code == 401  # the old one stays dead
        assert _state(h, again["proposalId"]) == "ready"
        assert h.repository.verify_admin_audit_chain().ok


def test_removing_one_of_two_connections_leaves_the_other_working(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        _, _, address, token = _connect(h, readOnly=True)
        first = _connection(h)
        run_cli(
            h.database, h.config_path, "link", "--server-id", "learn", "--label", "second",
            "--credential", "none",
        )  # fmt: skip
        assert len(h.repository.list_connections("learn")) == 2

        assert h.ask("remove_connection", {"connectionId": first}).status_code == 200

        assert h.repository.get_server("learn").enabled is True  # another still uses it
        assert _call(h, address, token, "list_issues").status_code == 401
        listed = h.ask("list_connections").json()["structuredContent"]["connections"]
        assert [item["label"] for item in listed] == ["second"]


def test_the_setup_connection_itself_cannot_be_changed_or_removed_from_chat(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        setup = h.repository.list_connections("adapter-setup")[0].connection_id
        for operation, body in (
            ("remove_connection", {"connectionId": setup}),
            ("turn_off_operations", {"connectionId": setup, "operations": ["find_server"]}),
            ("expire_access", {"connectionId": setup, "hours": 1}),
            (
                "limit_arguments",
                {
                    "connectionId": setup,
                    "limitArguments": [
                        {"operation": "*", "argument": "query", "allowedValues": ["a"]}
                    ],
                },
            ),
            ("propose_change", {"connectionId": setup, "userWords": "x", "expiresInHours": 1}),
        ):
            answer = h.ask(operation, body)
            assert answer.status_code == 409, (operation, answer.text)
            assert "setup connection cannot be changed" in answer.json()["message"]

        assert h.ask("find_server", {"query": "linear"}).status_code == 200  # nothing was touched


def test_a_connection_that_is_gone_cannot_be_changed_or_asked_about(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        _connect(h, readOnly=True)
        connection = _connection(h)
        assert h.ask("remove_connection", {"connectionId": connection}).status_code == 200

        for operation, body in (
            ("remove_connection", {"connectionId": connection}),
            ("turn_off_operations", {"connectionId": connection, "operations": ["list_issues"]}),
            ("expire_access", {"connectionId": connection, "hours": 1}),
            ("explain_connection", {"connectionId": connection}),
            ("propose_change", {"connectionId": connection, "userWords": "x", "expiresInHours": 1}),
        ):
            answer = h.ask(operation, body)
            assert answer.status_code == 404, (operation, answer.text)
            assert (
                "no longer exists" in answer.json()["message"]
                or "does not exist" in answer.json()["message"]
            )


def test_nothing_to_change_and_nothing_known_are_each_said_plainly(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        _connect(h, readOnly=True)
        connection = _connection(h)

        empty = h.ask("propose_change", {"connectionId": connection, "userWords": "change it"})
        assert empty.status_code == 400
        assert "nothing to add or change" in empty.json()["message"]

        unknown = h.ask(
            "propose_change",
            {"connectionId": "conn_000000000000", "userWords": "x", "expiresInHours": 1},
        )
        assert unknown.status_code == 404

        assert len(h.ask("list_connections").json()["structuredContent"]["proposalsWaiting"]) == 0


def test_switching_off_everything_leaves_a_connection_that_offers_nothing(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        _, _, address, token = _connect(h, readOnly=True)
        connection = _connection(h)
        offered = [
            item["id"]
            for item in h.ask("list_connections").json()["structuredContent"]["connections"][0][
                "operations"
            ]
        ]
        assert offered

        off = h.ask("turn_off_operations", {"connectionId": connection, "operations": offered})

        assert off.status_code == 200, off.text
        for operation in offered:
            assert _call(h, address, token, operation).status_code == 404
        assert (
            h.ask("list_connections").json()["structuredContent"]["connections"][0]["operations"]
            == []
        )
        assert h.repository.verify_admin_audit_chain().ok


# --- two services side by side -----------------------------------------------------------


def test_narrowing_one_service_does_not_touch_another(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        _, _, learn_address, learn_token = _connect(h, readOnly=True)
        other = h.ask(
            "propose_connection",
            {
                "endpoint": "https://other.example/mcp",
                "readOnly": True,
                "userWords": "the other one",
            },
        ).json()["structuredContent"]
        page = h.review(other["reviewLink"], plan="none")
        assert h.approve(other["reviewLink"], page).status_code == 302
        other_address, other_token = connector_from(h.get(other["reviewLink"]).text)
        assert other_address != learn_address
        assert _call(h, other_address, other_token, "list_issues").status_code == 200

        learn = _connection(h, "learn")
        assert (
            h.ask(
                "turn_off_operations", {"connectionId": learn, "operations": ["list_issues"]}
            ).status_code
            == 200
        )
        h.clock.advance(60)
        assert h.ask("expire_access", {"connectionId": learn, "hours": 1}).status_code == 200

        assert _call(h, learn_address, learn_token, "list_issues").status_code == 404
        assert _call(h, other_address, other_token, "list_issues").status_code == 200
        names = {
            item["serverId"]
            for item in h.ask("list_connections").json()["structuredContent"]["connections"]
        }
        assert names == {"learn", "other-example"}


def test_a_second_request_for_a_service_already_connected_is_stopped_at_the_owner(
    tmp_path: Path,
) -> None:
    with chat_harness(tmp_path) as h:
        first = h.propose(userWords="first request")
        second = h.propose(userWords="a second request for the same service")
        assert h.approve(first["reviewLink"], h.review(first["reviewLink"])).status_code == 302

        h.unlock(second["reviewLink"])
        started = h.post(f"{_path(second['reviewLink'])}/start")

        assert "learn is already connected" in _text(started.text)
        assert _state(h, second["proposalId"]) == "waiting_for_owner"
        assert len(h.repository.list_connections("learn")) == 1


# --- a restart in the middle -------------------------------------------------------------


@contextmanager
def _second_application(h: ChatHarness) -> Generator[tuple[TestClient, TestClient]]:
    """The adapter started again over the same database, as after a restart."""
    with SqliteControlPlaneRepository(h.database) as repository:
        repository.initialize()
        app = build_application(
            repository,
            load_serve_config(h.config_path),
            {},
            database=h.database,
            mcp_client=h.tools,  # type: ignore[arg-type]
            clock=h.clock,
            discovery=h.discovery,
        )
        with (
            TestClient(app, base_url=PUBLIC_URL) as agent,
            TestClient(app, base_url=PUBLIC_URL) as browser,
        ):
            yield agent, browser


def test_after_a_restart_connections_limits_and_waiting_proposals_are_all_still_there(
    tmp_path: Path,
) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        _, _, address, token = _connect(h, readOnly=True, expiresInHours=2)
        connection = _connection(h)
        limit = {"operation": "*", "argument": "project", "allowedValues": ["ENG"]}
        assert (
            h.ask(
                "limit_arguments", {"connectionId": connection, "limitArguments": [limit]}
            ).status_code
            == 200
        )
        assert (
            h.ask(
                "turn_off_operations", {"connectionId": connection, "operations": ["search_notes"]}
            ).status_code
            == 200
        )
        pending = h.ask(
            "propose_change",
            {
                "connectionId": connection,
                "userWords": "also create issues",
                "alsoAllow": ["create_issue"],
            },
        ).json()["structuredContent"]

        with _second_application(h) as (agent, browser):
            base = _path(address).removesuffix("/openapi.json")
            headers = {"Authorization": f"Bearer {token}"}
            inside = agent.post(
                f"{base}/operations/list_issues", json={"project": "ENG"}, headers=headers
            )
            outside = agent.post(
                f"{base}/operations/list_issues", json={"project": "OPS"}, headers=headers
            )
            gone = agent.post(
                f"{base}/operations/search_notes", json={"query": "x"}, headers=headers
            )
            assert (inside.status_code, outside.status_code, gone.status_code) == (200, 400, 404)

            # the setup connector and the proposal that was waiting survived too
            setup = agent.post(
                f"/c/{h.setup_slug}/operations/get_proposal_status",
                json={"proposalId": pending["proposalId"]},
                headers={"Authorization": f"Bearer {h.setup_token}"},
            )
            assert setup.json()["structuredContent"]["state"] == "waiting_for_owner"

            # the owner's code still opens the old link, and the access still ends when it said
            path = _path(pending["reviewLink"])
            browser.get(path)
            csrf = browser.cookies.get(CSRF_COOKIE)
            unlocked = browser.post(
                f"{path}/unlock",
                data={"csrf": csrf, "code": h.owner_code},
                follow_redirects=False,
            )
            assert unlocked.status_code == 302

        h.clock.advance(3 * 3600)
        assert _call(h, address, token, "list_issues", project="ENG").status_code == 403


# --- every step after the code needs the session and the token ---------------------------


@pytest.mark.parametrize("route", ["start", "discover", "cancel", "decide", "collected"])
def test_a_step_without_a_session_goes_back_to_the_code_and_one_without_a_token_is_refused(
    tmp_path: Path, route: str
) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose()
        path = _path(proposal["reviewLink"])
        h.get(path)  # the page hands the browser its cross-site token

        no_session = h.post(f"{path}/{route}")
        assert no_session.status_code == 302 and no_session.headers["location"] == path

        h.unlock(proposal["reviewLink"])
        no_token = h.browser.post(
            f"{path}/{route}", data={"action": "approve"}, follow_redirects=False
        )
        wrong_token = h.browser.post(
            f"{path}/{route}", data={"csrf": "x" * 32, "action": "approve"}, follow_redirects=False
        )
        assert no_token.status_code == 403 and wrong_token.status_code == 403
        assert _state(h, proposal["proposalId"]) == "waiting_for_owner"
        with pytest.raises(EntityNotFoundError):
            h.repository.get_active_catalog("learn")  # nothing was put in place


def test_a_server_registered_at_the_command_line_is_adopted_when_the_address_is_the_same(
    tmp_path: Path,
) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        run_cli(
            h.database, h.config_path, "server-register", "--server-id", "learn",
            "--display-name", "Microsoft Learn", "--endpoint", "https://learn.microsoft.com/api/mcp",
            "--auth-profile", "none",
        )  # fmt: skip
        assert h.repository.get_server("learn").enabled is True

        _, _, address, token = _connect(h, readOnly=True)

        assert _call(h, address, token, "list_issues").status_code == 200
        assert len(h.repository.list_connections("learn")) == 1
