"""The commands the owner runs on the adapter's machine for chat-driven setup."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from muse_mcp_adapter.cli import main
from tests.fixtures.chat_setup import chat_harness, connector_from, run_cli


def test_enabling_is_safe_to_repeat_and_shows_secrets_only_once(tmp_path: Path) -> None:
    database, config = tmp_path / "adapter.db", tmp_path / "serve.json"
    config.write_text(json.dumps({"publicUrl": "https://adapter.example.com"}))
    first = run_cli(database, config, "chat-setup-enable")
    assert first["ownerCode"] and first["token"] and "Adapter setup" in first["museInstructions"]
    assert "chatSetup" in first["warning"]  # the pages are off until the service is told

    again = run_cli(database, config, "chat-setup-enable")
    assert "ownerCode" not in again and "token" not in again
    assert "already" in again["connectorNote"] and "already set" in again["ownerCodeNote"]
    assert again["connectorUrl"] == first["connectorUrl"]

    config.write_text(json.dumps({"publicUrl": "https://adapter.example.com", "chatSetup": True}))
    rotated = run_cli(database, config, "chat-setup-enable", "--rotate-owner-code")
    assert rotated["ownerCode"] != first["ownerCode"] and "warning" not in rotated


def test_a_rotated_owner_code_stops_the_old_one_and_ends_open_sessions(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        link = h.propose(readOnly=True)["reviewLink"]
        old = h.owner_code
        assert h.unlock(link, old).status_code == 302
        assert h.get(link).status_code == 200  # unlocked

        new = run_cli(h.database, h.config_path, "chat-setup-enable", "--rotate-owner-code")
        assert h.get(link).text.count("owner code") >= 1  # the open session ended
        assert h.unlock(link, old).status_code == 403
        assert h.unlock(link, str(new["ownerCode"])).status_code == 302


def test_the_owner_can_list_proposals_and_ask_for_a_fresh_link(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose(readOnly=True, userWords="a" * 300)
        listed = run_cli(h.database, h.config_path, "proposals")
        assert [item["proposalId"] for item in listed] == [proposal["proposalId"]]
        assert len(listed[0]["userWords"]) == 120

        fresh = run_cli(
            h.database, h.config_path, "proposal-link", "--proposal-id", proposal["proposalId"]
        )
        assert h.get(proposal["reviewLink"]).status_code == 404  # the agent's link stopped
        page = h.review(fresh["reviewLink"])
        assert h.approve(fresh["reviewLink"], page).status_code == 302
        done = run_cli(h.database, h.config_path, "proposals")
        assert done == []
        everything = run_cli(h.database, h.config_path, "proposals", "--all")
        assert everything[0]["state"] == "applied"
        address, token = connector_from(h.get(fresh["reviewLink"]).text)
        assert address.endswith("/openapi.json") and token


def test_the_record_of_a_change_and_of_a_call_can_be_read_back(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose(readOnly=True, userWords="Read only please.")
        h.approve(proposal["reviewLink"], h.review(proposal["reviewLink"]))
        record = run_cli(
            h.database, h.config_path, "explain-change", "--proposal-id", proposal["proposalId"]
        )
        assert record["userWords"] == "Read only please." and record["state"] == "applied"
        assert record["decision"]["method"] == "owner-code"
        assert record["applied"]["catalogVersion"].startswith("chat-")


def test_a_catalog_published_at_the_command_line_has_no_chat_request(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        # the setup connector's own catalog was published by the adapter, not by a proposal
        h.ask("how_setup_works")
        request_id = h.repository.list_runtime_audit()[-1]["request_id"]
        trace = run_cli(h.database, h.config_path, "explain-call", "--request-id", str(request_id))
        assert trace["proposal"] is None and "no chat request" in trace["note"]


def test_audit_verify_passes_and_fails_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with chat_harness(tmp_path) as h:
        h.approve(*(lambda link: (link, h.review(link)))(h.propose(readOnly=True)["reviewLink"]))
        verified = run_cli(h.database, h.config_path, "audit-verify")
        assert verified["ok"] is True and verified["chainedRows"] > 5 and verified["head"]
        capsys.readouterr()

        raw = sqlite3.connect(h.database)
        raw.execute("UPDATE admin_audit SET action = 'forged' WHERE event_id = 3")
        raw.commit()
        raw.close()
        code = main(["--database", str(h.database), "--config", str(h.config_path), "audit-verify"])
        assert code == 1
        assert "broken at event 3" in capsys.readouterr().err


def test_unknown_things_are_reported_not_crashed_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with chat_harness(tmp_path) as h:
        base = ["--database", str(h.database), "--config", str(h.config_path)]
        capsys.readouterr()
        assert main([*base, "explain-call", "--request-id", "nope"]) == 1
        assert main([*base, "explain-change", "--proposal-id", "prop_nope"]) == 1
        assert main([*base, "proposal-link", "--proposal-id", "prop_nope"]) == 1
        assert capsys.readouterr().err.count("error") >= 3
