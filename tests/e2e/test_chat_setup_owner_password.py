"""Approving anything that adds access takes the owner's password, typed every time.

A session from unlocking a page lets you read pages. It never approves, so a browser that someone
else can drive (or a page left open) cannot widen access without the password.
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.chat_setup import (
    chat_harness,
    digest_of,
    projects_manifest,
    run_cli,
    ticked,
)

ENDPOINT = "https://tracker.example/mcp"


def _path(link: str) -> str:
    return re.sub(r"^https?://[^/]+", "", link)


def _open(h: Any, **fields: object) -> dict[str, Any]:
    body: dict[str, object] = {"endpoint": ENDPOINT, "userWords": "connect it", "readOnly": True}
    body.update(fields)
    reply = h.ask("propose_connection", body)
    assert reply.status_code == 200, reply.text
    return reply.json()["structuredContent"]  # type: ignore[no-any-return]


def test_an_unlocked_session_alone_cannot_approve(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        proposal = _open(h)
        page = h.review(proposal["reviewLink"], plan="none")  # unlocked, even started
        refused = h.post(
            f"{_path(proposal['reviewLink'])}/decide",
            action="approve",
            digest=digest_of(page),
            chosen=ticked(page),
        )
        assert refused.status_code == 200 and "owner password" in refused.text
        assert h.status(proposal["proposalId"])["state"] == "in_review"


def test_a_wrong_password_is_refused_and_counts_toward_the_lock(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        proposal = _open(h)
        link = proposal["reviewLink"]
        page = h.review(link, plan="none")
        for _ in range(5):
            wrong = h.approve(link, page, code="not the password")
            assert wrong.status_code == 200 and "not right" in wrong.text
        locked = h.approve(link, page)  # even the right one, now
        assert locked.status_code == 200 and "Too many" in locked.text
        assert h.status(proposal["proposalId"])["state"] == "in_review"


def test_the_right_password_approves(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        proposal = _open(h)
        page = h.review(proposal["reviewLink"], plan="none")
        assert h.approve(proposal["reviewLink"], page).status_code == 302
        assert h.status(proposal["proposalId"])["state"] == "ready"


def test_declining_needs_no_password(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        proposal = _open(h)
        page = h.review(proposal["reviewLink"], plan="none")
        link = _path(proposal["reviewLink"])
        assert h.post(f"{link}/decide", action="decline", digest=digest_of(page)).status_code == 302
        assert h.status(proposal["proposalId"])["state"] == "declined"


def test_widening_a_live_connection_also_needs_the_password(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        first = _open(h)
        assert (
            h.approve(first["reviewLink"], h.review(first["reviewLink"], plan="none")).status_code
            == 302
        )
        connection = h.ask("list_connections", {}).json()["structuredContent"]["connections"][0][
            "connectionId"
        ]
        widen = h.ask(
            "propose_change",
            {
                "connectionId": connection,
                "userWords": "also create issues",
                "alsoAllow": ["create_issue"],
            },
        )
        assert widen.status_code == 200, widen.text
        link = widen.json()["structuredContent"]["reviewLink"]
        h.get(link)  # the session from before is still valid: no prompt for the page
        page = h.get(link).text
        if "Approve" not in page:  # a change starts with a step before the list
            h.post(f"{_path(link)}/start")
            h.post(f"{_path(link)}/discover")
            page = h.get(link).text
        path = _path(link)
        refused = h.post(
            f"{path}/decide", action="approve", digest=digest_of(page), chosen=ticked(page)
        )
        assert "owner password" in refused.text
        done = h.post(
            f"{path}/decide",
            action="approve",
            digest=digest_of(page),
            chosen=ticked(page),
            code=h.owner_code,
        )
        assert done.status_code == 302


def test_a_chosen_password_replaces_the_generated_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        generated = h.owner_code
        monkeypatch.setattr(sys, "stdin", io.StringIO("my own long password\n"))
        result = run_cli(h.database, h.config_path, "chat-setup-set-password")
        assert result["ownerPasswordSet"] is True

        proposal = _open(h)
        link = proposal["reviewLink"]
        assert h.unlock(link, generated).status_code == 403  # the old code is dead
        assert h.unlock(link, "my own long password").status_code == 302
        h.get(link)
        assert h.post(f"{_path(link)}/start", plan="none").status_code == 302
        page = h.get(link).text
        assert h.approve(link, page, code=generated).status_code == 200
        assert h.approve(link, page, code="MY OWN LONG PASSWORD").status_code == 200  # exact
        assert h.approve(link, page, code="my own long password").status_code == 302


def test_a_short_password_is_refused_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        monkeypatch.setattr(sys, "stdin", io.StringIO("short\n"))
        from muse_mcp_adapter.cli import main

        code = main(
            [
                "--database",
                str(h.database),
                "--config",
                str(h.config_path),
                "chat-setup-set-password",
            ]
        )
        assert code != 0
        proposal = _open(h)
        page = h.review(proposal["reviewLink"], plan="none")
        assert (
            h.approve(proposal["reviewLink"], page).status_code == 302
        )  # the old code still works
