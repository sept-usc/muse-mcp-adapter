"""Two things the owner's pages must get right when more than one request is open.

A second page opened in the same browser must not make the first page's form stale, and a request
that stopped must say why once.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.fixtures.chat_setup import CSRF_COOKIE, chat_harness

CSRF_FIELD = re.compile(r"name=csrf value='([^']+)'")
STALE = "This form has expired"


def _path(link: str) -> str:
    return re.sub(r"^https?://[^/]+", "", link)


def test_a_second_review_page_does_not_make_the_first_one_stale(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        first = h.propose(userWords="connect it, read only")
        second = h.propose(userWords="a different request")
        assert h.unlock(first["reviewLink"]).status_code == 302
        page = h.get(first["reviewLink"])
        shown = CSRF_FIELD.search(page.text)
        assert shown is not None
        held = h.browser.cookies.get(CSRF_COOKIE)

        h.get(second["reviewLink"])  # the owner opens the other request in another tab

        assert h.browser.cookies.get(CSRF_COOKIE) == held  # the browser keeps the token it had
        pressed = h.browser.post(
            f"{_path(first['reviewLink'])}/start",
            data={"csrf": shown.group(1), "plan": "none"},
            follow_redirects=False,
        )
        assert pressed.status_code == 302, pressed.text
        assert STALE not in pressed.text


def test_a_form_without_the_browsers_token_is_still_refused(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose()
        assert h.unlock(proposal["reviewLink"]).status_code == 302
        h.get(proposal["reviewLink"])

        forged = h.browser.post(
            f"{_path(proposal['reviewLink'])}/start",
            data={"csrf": "x" * 32, "plan": "none"},
            follow_redirects=False,
        )

        assert forged.status_code == 403
        assert STALE in forged.text


def test_a_change_that_could_not_be_put_in_place_says_why_once(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        first = h.propose(readOnly=True)
        assert h.approve(first["reviewLink"], h.review(first["reviewLink"])).status_code == 302
        connection = h.repository.list_connections("learn")[0].connection_id
        change = h.ask(
            "propose_change",
            {
                "connectionId": connection,
                "userWords": "also create issues",
                "alsoAllow": ["create_issue"],
            },
        ).json()["structuredContent"]
        page = h.review(change["reviewLink"])
        # the agent makes the connection smaller while the owner is reading
        narrowed = h.ask(
            "turn_off_operations", {"connectionId": connection, "operations": ["list_issues"]}
        )
        assert narrowed.status_code == 200

        answer = h.approve(change["reviewLink"], page)

        assert answer.status_code == 409
        text = " ".join(re.sub(r"<[^>]+>", " ", answer.text).split())
        assert text.count("What is live changed while you were reviewing") == 1
