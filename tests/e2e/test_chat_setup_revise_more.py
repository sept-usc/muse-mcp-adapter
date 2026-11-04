"""Revising a proposal: every path, every refusal, and what is recorded."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.chat_setup import chat_harness, digest_of, projects_manifest, run_cli, ticked

ENDPOINT = "https://tracker.example/mcp"


def _path(link: str) -> str:
    return re.sub(r"^https?://[^/]+", "", link)


def _open(h: Any, **fields: object) -> dict[str, Any]:
    body: dict[str, object] = {"endpoint": ENDPOINT, "userWords": "connect it"}
    body.update(fields)
    reply = h.ask("propose_connection", body)
    assert reply.status_code == 200, reply.text
    return reply.json()["structuredContent"]  # type: ignore[no-any-return]


def _names(page: str) -> set[str]:
    return {key.removeprefix("tool:") for key in ticked(page)}


def _many(count: int) -> dict[str, object]:
    manifest = projects_manifest()
    primitives: Any = manifest["primitives"]
    for index in range(count):
        primitives.append(
            {
                "type": "tool",
                "name": f"extra_{index:03d}",
                "description": "x",
                "inputSchema": {"type": "object", "properties": {}},
            }
        )
    return manifest


def test_only_the_requesting_connection_may_list_or_revise(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        proposal = _open(h, includeReads=True)
        h.review(proposal["reviewLink"], plan="none")
        other = h.service._connections.link(  # pyright: ignore[reportPrivateUsage]
            server_id="adapter-setup", label="other", credential_kind="none", actor="test"
        )
        for operation, extra in (
            ("list_server_tools", {}),
            ("revise_proposal", {"includeReads": True}),
        ):
            reply = h.agent.post(
                f"/c/{other.record.slug}/operations/{operation}",
                json={"proposalId": proposal["proposalId"], **extra},
                headers={"Authorization": f"Bearer {other.token}"},
            )
            assert reply.status_code == 404, (operation, reply.text)


def test_resources_and_prompts_are_not_listed_as_tools(tmp_path: Path) -> None:
    manifest = projects_manifest()
    primitives: Any = manifest["primitives"]
    primitives.append(
        {"type": "resource", "name": "readme", "uri": "mcp://docs/readme", "mimeType": "text/plain"}
    )
    primitives.append({"type": "prompt", "name": "triage", "arguments": []})
    with chat_harness(tmp_path, manifest=manifest) as h:
        proposal = _open(h, includeReads=True)
        h.review(proposal["reviewLink"], plan="none")
        body = h.ask("list_server_tools", {"proposalId": proposal["proposalId"]}).json()[
            "structuredContent"
        ]
        listed = {item["name"] for item in body["tools"]}
        assert "readme" not in listed and "triage" not in listed
        assert {"list_issues", "create_issue"} <= listed


def test_a_very_long_list_is_cut_and_the_cut_is_said(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=_many(305)) as h:
        proposal = _open(h)
        h.review(proposal["reviewLink"], plan="none")
        body = h.ask("list_server_tools", {"proposalId": proposal["proposalId"]}).json()[
            "structuredContent"
        ]
        assert len(body["tools"]) == 300
        assert (
            body["notShown"] == 4 + 305 - 300
        )  # the four base tools plus the 305 extras, less 300


@pytest.mark.parametrize(
    "bad",
    [
        {"includeReads": "yes"},
        {"readOnly": 1},
        {"alsoAllow": ["has a space"]},
        {"alsoAllow": "create_issue"},
        {"exclude": [""]},
        {"expiresInHours": 0},
        {"expiresInHours": 721},
        {"timeoutSeconds": 301},
        {"limitArguments": [{"operation": "*"}]},
        {"unknownField": True},
    ],
)
def test_a_malformed_revision_is_refused_and_changes_nothing(
    tmp_path: Path, bad: dict[str, Any]
) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        proposal = _open(h, includeReads=True)
        page = h.review(proposal["reviewLink"], plan="none")
        reply = h.ask("revise_proposal", {"proposalId": proposal["proposalId"], **bad})
        assert reply.status_code in (400, 422), (bad, reply.text)
        assert digest_of(h.get(proposal["reviewLink"]).text) == digest_of(page)


@pytest.mark.parametrize("closing", ["declined", "expired"])
def test_a_closed_proposal_cannot_be_listed_or_revised(tmp_path: Path, closing: str) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        proposal = _open(h, includeReads=True)
        link = proposal["reviewLink"]
        page = h.review(link, plan="none")
        if closing == "declined":
            assert (
                h.post(
                    f"{_path(link)}/decide", action="decline", digest=digest_of(page)
                ).status_code
                == 302
            )
        else:
            h.clock.advance(3 * 24 * 3600)
        for operation, extra in (
            ("list_server_tools", {}),
            ("revise_proposal", {"includeReads": False}),
        ):
            reply = h.ask(operation, {"proposalId": proposal["proposalId"], **extra})
            assert reply.status_code == 409, (operation, reply.text)


def test_a_revision_can_change_limits_and_the_end_time(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        proposal = _open(h, includeReads=True)
        h.review(proposal["reviewLink"], plan="none")
        reply = h.ask(
            "revise_proposal",
            {
                "proposalId": proposal["proposalId"],
                "expiresInHours": 3,
                "limitArguments": [
                    {"operation": "*", "argument": "project", "allowedValues": ["ENG"]}
                ],
                "timeoutSeconds": 20,
            },
        )
        assert reply.status_code == 200, reply.text
        page = h.get(proposal["reviewLink"]).text
        assert "ENG" in page and re.search(r"3 hours|ends", page)
        assert h.approve(proposal["reviewLink"], page).status_code == 302
        address, token = __import__("tests.fixtures.chat_setup", fromlist=["x"]).connector_from(
            h.get(proposal["reviewLink"]).text
        )
        refused = h.agent.post(
            f"{address.removesuffix('/openapi.json')}/operations/list_issues",
            json={"project": "OPS"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert refused.status_code in (400, 403, 422)  # the limit revised in is enforced


def test_includereads_and_readonly_together_and_an_exclusion_of_a_read(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        proposal = _open(h, readOnly=True, includeReads=True, exclude=["search_notes"])
        page = h.review(proposal["reviewLink"], plan="none")
        assert _names(page) == {"list_issues", "get_issue"}  # reads, less the one left out


def test_revising_a_change_proposal_recompiles_against_the_live_policy(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        first = _open(h, readOnly=True)
        assert (
            h.approve(first["reviewLink"], h.review(first["reviewLink"], plan="none")).status_code
            == 302
        )
        connection = h.ask("list_connections", {}).json()["structuredContent"]["connections"][0][
            "connectionId"
        ]

        only_reads = h.ask(
            "propose_change",
            {"connectionId": connection, "userWords": "reads again", "includeReads": True},
        )
        assert only_reads.status_code == 200, (
            only_reads.text
        )  # includeReads alone is something to change
        nothing = h.ask("propose_change", {"connectionId": connection, "userWords": "nothing"})
        assert nothing.status_code == 400 and "nothing to add" in nothing.text

        proposal = h.ask(
            "propose_change",
            {
                "connectionId": connection,
                "userWords": "also create issues",
                "alsoAllow": ["create_isue"],
            },
        ).json()["structuredContent"]
        link = proposal["reviewLink"]
        h.get(link)
        assert h.post(f"{_path(link)}/start").status_code == 302
        h.post(f"{_path(link)}/discover")
        page = h.get(link).text
        status = h.ask("get_proposal_status", {"proposalId": proposal["proposalId"]}).json()[
            "structuredContent"
        ]
        assert status["state"] == "in_review" and any(
            "create_isue" in item for item in status["notMatched"]
        )

        listed = h.ask("list_server_tools", {"proposalId": proposal["proposalId"]}).json()[
            "structuredContent"
        ]
        assert any(
            t["name"] == "list_issues" and t["selected"] for t in listed["tools"]
        )  # kept live
        revised = h.ask(
            "revise_proposal", {"proposalId": proposal["proposalId"], "alsoAllow": ["create_issue"]}
        )
        assert revised.status_code == 200, revised.text
        assert "create_issue" in revised.json()["structuredContent"]["selected"]
        new_page = h.get(link).text
        assert digest_of(new_page) != digest_of(page)
        assert h.approve(link, new_page).status_code == 302


def test_a_revision_is_recorded_and_the_audit_chain_still_verifies(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        proposal = _open(h, includeReads=True)
        h.review(proposal["reviewLink"], plan="none")
        h.ask(
            "revise_proposal", {"proposalId": proposal["proposalId"], "alsoAllow": ["create_issue"]}
        )
        db = sqlite3.connect(h.database)
        rows = db.execute(
            "SELECT actor, action, data_json FROM admin_audit "
            "WHERE action = 'setup.proposal.revised'"
        ).fetchall()
        db.close()
        assert len(rows) == 1 and rows[0][0] == "chat-setup:agent"
        data = json.loads(rows[0][2])
        assert data["fields"] == ["alsoAllow"] and data["proposalId"] == proposal["proposalId"]
        assert run_cli(h.database, h.config_path, "audit-verify")["ok"] is True


def test_the_final_approval_exposes_exactly_what_the_last_revision_selected(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        proposal = _open(h, includeReads=True, alsoAllow=["create_issue"])
        link = proposal["reviewLink"]
        h.review(link, plan="none")
        for names in (["create_issue"], [], ["create_issue", "search_notes"]):
            h.ask(
                "revise_proposal",
                {"proposalId": proposal["proposalId"], "alsoAllow": names, "exclude": []},
            )
        page = h.get(link).text
        assert h.approve(link, page).status_code == 302
        status = h.ask("get_proposal_status", {"proposalId": proposal["proposalId"]}).json()[
            "structuredContent"
        ]
        exposed = {op["id"] for op in status["connector"]["operations"]}
        assert exposed == {"list_issues", "get_issue", "search_notes", "create_issue"}
