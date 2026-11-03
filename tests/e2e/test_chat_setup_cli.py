"""The commands the owner runs on the adapter's machine for chat-driven setup."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from muse_mcp_adapter.cli import main
from tests.fixtures.chat_setup import (
    chat_harness,
    connector_from,
    projects_manifest,
    run_cli,
)


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


def test_a_call_after_an_agent_narrowing_is_traced_to_that_narrowing(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        link = h.propose(readOnly=True)["reviewLink"]
        h.approve(link, h.review(link))
        address, token = connector_from(h.get(link).text)
        connection = h.repository.list_connections("learn")[0].connection_id
        narrowed = h.ask(
            "turn_off_operations", {"connectionId": connection, "operations": ["get_issue"]}
        )
        assert narrowed.status_code == 200

        base = re.sub(r"^https?://[^/]+", "", address).removesuffix("/openapi.json")
        called = h.agent.post(
            f"{base}/operations/list_issues",
            json={"query": "x"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert called.status_code == 200
        request_id = str(h.repository.list_runtime_audit()[-1]["request_id"])

        trace = run_cli(h.database, h.config_path, "explain-call", "--request-id", request_id)
        assert trace["proposal"] is None
        assert "narrowed this connection" in trace["note"]
        assert "command line" not in trace["note"]
        assert trace["change"]["action"] == "setup.catalog.narrowed"
        assert trace["change"]["actor"] == "chat-setup:agent"
        assert trace["change"]["data"]["removed"] == ["get_issue"]


def test_waiting_proposals_can_be_closed_from_the_command_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with chat_harness(tmp_path) as h:
        first = h.propose(readOnly=True, userWords="first")["proposalId"]
        second = h.propose(readOnly=True, userWords="second")["proposalId"]
        third = h.propose(readOnly=True, userWords="third")["proposalId"]

        closed = run_cli(h.database, h.config_path, "proposal-decline", "--proposal-id", first)
        assert closed["declined"] == [first]
        states = {
            item["proposalId"]: item["state"]
            for item in run_cli(h.database, h.config_path, "proposals", "--all")
        }
        assert states == {
            first: "declined",
            second: "waiting_for_owner",
            third: "waiting_for_owner",
        }

        rest = run_cli(h.database, h.config_path, "proposal-decline", "--all")
        assert sorted(rest["declined"]) == sorted([second, third])
        assert run_cli(h.database, h.config_path, "proposals") == []

        # the agent is told, so it cannot claim success
        status = h.ask("get_proposal_status", {"proposalId": first})
        assert "declined" in json.dumps(status.json())

        entries = [
            entry
            for entry in h.repository.list_admin_audit()
            if entry["action"] == "setup.proposal.declined"
        ]
        assert len(entries) == 3
        assert {cast(dict[str, object], e["data"])["via"] for e in entries} == {"command line"}
        assert h.repository.verify_admin_audit_chain().ok

        base = ["--database", str(h.database), "--config", str(h.config_path)]
        capsys.readouterr()
        assert main([*base, "proposal-decline"]) == 1  # neither an id nor --all
        assert main([*base, "proposal-decline", "--all", "--proposal-id", first]) == 1  # both
        assert main([*base, "proposal-decline", "--proposal-id", "prop_nope"]) == 1  # unknown
        assert capsys.readouterr().err.count("error") >= 3


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


def test_a_noted_head_catches_rows_cut_off_the_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with chat_harness(tmp_path) as h:
        link = h.propose(readOnly=True)["reviewLink"]
        page = h.review(link)
        noted = run_cli(h.database, h.config_path, "audit-verify")["head"]
        h.approve(link, page)  # more events after the head was noted down

        verified = run_cli(h.database, h.config_path, "audit-verify", "--head", noted)
        assert verified["pinnedHead"]["found"] is True and verified["pinnedHead"]["rowsSince"] > 0
        assert "pinnedHead" not in run_cli(h.database, h.config_path, "audit-verify")
        capsys.readouterr()

        raw = sqlite3.connect(h.database)
        event = raw.execute("SELECT event_id FROM admin_audit WHERE entry_hash = ?", (noted,))
        first = event.fetchone()[0]
        raw.execute("DELETE FROM admin_audit WHERE event_id >= ?", (first,))
        raw.commit()
        raw.close()
        arguments = ["--database", str(h.database), "--config", str(h.config_path), "audit-verify"]
        assert main(arguments) == 0  # on its own the shorter chain looks fine
        capsys.readouterr()
        assert main([*arguments, "--head", noted]) == 1
        assert "not in the audit chain" in capsys.readouterr().err
