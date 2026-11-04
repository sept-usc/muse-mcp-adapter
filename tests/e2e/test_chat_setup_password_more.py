"""The owner password at approval: every refusal, every counter, and what is recorded."""

from __future__ import annotations

import io
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from muse_mcp_adapter.cli import main
from tests.fixtures.chat_setup import chat_harness, digest_of, projects_manifest, run_cli, ticked

ENDPOINT = "https://tracker.example/mcp"


def _path(link: str) -> str:
    return re.sub(r"^https?://[^/]+", "", link)


def _open(h: Any, **fields: object) -> dict[str, Any]:
    body: dict[str, object] = {"endpoint": ENDPOINT, "userWords": "connect it", "readOnly": True}
    body.update(fields)
    reply = h.ask("propose_connection", body)
    assert reply.status_code == 200, reply.text
    return reply.json()["structuredContent"]  # type: ignore[no-any-return]


def _state(h: Any, proposal: dict[str, Any]) -> str:
    return str(h.status(proposal["proposalId"])["state"])


def _audit(h: Any) -> list[tuple[str, str]]:
    db = sqlite3.connect(h.database)
    rows = db.execute("SELECT actor, action FROM admin_audit").fetchall()
    db.close()
    return [(a, b) for a, b in rows]


@pytest.mark.parametrize("blank", ["", " ", "   ", "\t"])
def test_an_empty_or_blank_password_is_refused_without_counting(tmp_path: Path, blank: str) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        proposal = _open(h)
        page = h.review(proposal["reviewLink"], plan="none")
        refused = h.approve(proposal["reviewLink"], page, code=blank or " ")
        assert refused.status_code == 200 and "owner password" in refused.text
        assert _state(h, proposal) == "in_review"
        # blank answers are not guesses: five of them do not lock the link
        for _ in range(6):
            h.approve(proposal["reviewLink"], page, code=blank or " ")
        assert h.approve(proposal["reviewLink"], page).status_code == 302


def test_the_form_asks_for_the_password_in_a_password_field(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        proposal = _open(h)
        page = h.review(proposal["reviewLink"], plan="none")
        assert re.search(r"<input type=password name=code[^>]*autocomplete=off", page)
        assert "Declining needs none" in page


def test_a_stale_page_is_refused_before_the_password_is_looked_at(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        proposal = _open(h)
        link = proposal["reviewLink"]
        h.review(link, plan="none")
        stale = h.post(
            f"{_path(link)}/decide",
            action="approve",
            digest="sha256:" + "0" * 64,
            chosen=[],
            code="wrong on purpose",
        )
        assert "has changed" in stale.text  # the digest is checked first, so no wrong-code count
        page = h.get(link).text
        assert h.approve(link, page).status_code == 302  # and nothing was locked


def test_the_lock_belongs_to_one_proposal_and_a_right_password_resets_it(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        first = _open(h, userWords="first")
        second = _open(h, endpoint="https://other.example/mcp", userWords="second")
        page_a = h.review(first["reviewLink"], plan="none")
        page_b = h.review(second["reviewLink"], plan="none")
        for _ in range(5):
            h.approve(first["reviewLink"], page_a, code="wrong")
        assert "Too many" in h.approve(first["reviewLink"], page_a).text  # A is locked
        assert h.approve(second["reviewLink"], page_b).status_code == 302  # B is not

    (tmp_path / "again").mkdir()
    with chat_harness(tmp_path / "again", manifest=projects_manifest()) as h:
        proposal = _open(h)
        page = h.review(proposal["reviewLink"], plan="none")
        for _ in range(4):
            h.approve(proposal["reviewLink"], page, code="wrong")
        # a right password on the fifth try resets the count, so a fresh run of four is allowed
        assert h.approve(proposal["reviewLink"], page).status_code == 302


def test_declining_neither_needs_nor_counts_a_password(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        proposal = _open(h)
        link = proposal["reviewLink"]
        page = h.review(link, plan="none")
        for _ in range(8):
            h.post(f"{_path(link)}/decide", action="decline", digest="sha256:" + "0" * 64)
        assert (
            h.post(f"{_path(link)}/decide", action="decline", digest=digest_of(page)).status_code
            == 302
        )
        assert _state(h, proposal) == "declined"


def test_the_generated_code_is_forgiving_but_a_chosen_password_is_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        proposal = _open(h)
        page = h.review(proposal["reviewLink"], plan="none")
        sloppy = h.owner_code.lower().replace("-", " ")
        assert h.approve(proposal["reviewLink"], page, code=sloppy).status_code == 302
    (tmp_path / "pw").mkdir()
    with chat_harness(tmp_path / "pw", manifest=projects_manifest()) as h:
        monkeypatch.setattr(sys, "stdin", io.StringIO("Exact Case Pass 1\n"))
        run_cli(h.database, h.config_path, "chat-setup-set-password")
        proposal = _open(h)
        link = proposal["reviewLink"]
        assert h.unlock(link, "Exact Case Pass 1").status_code == 302
        h.get(link)
        h.post(f"{_path(link)}/start", plan="none")
        page = h.get(link).text
        assert h.approve(link, page, code="exact case pass 1").status_code == 200
        assert h.approve(link, page, code="Exact Case Pass 1 ").status_code == 200
        assert h.approve(link, page, code="Exact Case Pass 1").status_code == 302


def test_setting_a_password_ends_open_sessions_and_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        proposal = _open(h)
        link = proposal["reviewLink"]
        assert h.unlock(link).status_code == 302
        assert "owner code" not in h.get(link).text  # signed in: the page is shown, not the prompt
        monkeypatch.setattr(sys, "stdin", io.StringIO("brand new password\n"))
        result = run_cli(h.database, h.config_path, "chat-setup-set-password")
        assert result["ownerPasswordSet"] is True and "brand new password" not in json.dumps(result)
        assert "owner code" in h.get(link).text  # the old session is gone: the page asks again
        assert any(action == "setup.owner_password.set" for _, action in _audit(h))
        assert run_cli(h.database, h.config_path, "audit-verify")["ok"] is True


@pytest.mark.parametrize(
    "typed",
    [
        "",
        "\n",
        "short\n",
        "123456789\n",
        "x" * 201 + "\n",
        "bell\x07 inside long\n",
        "tab\tinside long\n",
    ],
)
def test_a_bad_password_is_refused_and_nothing_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], typed: str
) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        monkeypatch.setattr(sys, "stdin", io.StringIO(typed))
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
        assert not any(a == "setup.owner_password.set" for _, a in _audit(h))
        proposal = _open(h)
        page = h.review(proposal["reviewLink"], plan="none")
        assert (
            h.approve(proposal["reviewLink"], page).status_code == 302
        )  # the old code still works
        capsys.readouterr()


def test_rotating_the_code_replaces_a_chosen_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        monkeypatch.setattr(sys, "stdin", io.StringIO("a chosen password\n"))
        run_cli(h.database, h.config_path, "chat-setup-set-password")
        rotated = run_cli(h.database, h.config_path, "chat-setup-enable", "--rotate-owner-code")
        proposal = _open(h)
        link = proposal["reviewLink"]
        assert h.unlock(link, "a chosen password").status_code == 403
        assert h.unlock(link, str(rotated["ownerCode"])).status_code == 302


def test_wrong_passwords_at_approval_are_recorded_and_the_lock_too(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        proposal = _open(h)
        page = h.review(proposal["reviewLink"], plan="none")
        for _ in range(5):
            h.approve(proposal["reviewLink"], page, code="nope")
        actions = [a for _, a in _audit(h)]
        assert actions.count("setup.approve.failed") == 5
        assert actions.count("setup.approve.locked") == 1
        assert "setup.proposal.approved" not in actions


def test_an_approval_is_recorded_only_after_the_password_was_right(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        proposal = _open(h)
        page = h.review(proposal["reviewLink"], plan="none")
        h.approve(proposal["reviewLink"], page, code="nope")
        assert "setup.proposal.approved" not in [a for _, a in _audit(h)]
        assert h.approve(proposal["reviewLink"], page).status_code == 302
        assert "setup.proposal.approved" in [a for _, a in _audit(h)]


def test_the_session_cookie_alone_never_approves_even_for_a_proposal_with_nothing_new(
    tmp_path: Path,
) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        proposal = _open(h)
        link = proposal["reviewLink"]
        page = h.review(link, plan="none")
        no_field = h.post(
            f"{_path(link)}/decide", action="approve", digest=digest_of(page), chosen=ticked(page)
        )
        assert no_field.status_code == 200 and _state(h, proposal) == "in_review"
