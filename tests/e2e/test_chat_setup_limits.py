"""Limits on what an operation may be called with: asked in words, approved by the owner, enforced.

"Only project ENG" is not a choice of operations, so it used to be written down as not enforced.
Now it becomes a limit in the policy the owner approves, and every call is held to it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tests.fixtures.chat_setup import (
    ChatHarness,
    chat_harness,
    connector_from,
    digest_of,
    projects_manifest,
    ticked,
)

ENG = {"operation": "*", "argument": "project", "allowedValues": ["ENG"]}


def _path(address: str) -> str:
    return re.sub(r"^https?://[^/]+", "", address)


def _connect(h: ChatHarness, **fields: object) -> tuple[dict[str, Any], str, str, str]:
    proposal = h.propose(**fields)
    link = proposal["reviewLink"]
    page = h.review(link)
    assert h.approve(link, page).status_code == 302
    address, token = connector_from(h.get(link).text)
    return proposal, link, address, token


def _call(h: ChatHarness, address: str, token: str, operation: str, **arguments: str) -> Any:
    base = _path(address).removesuffix("/openapi.json")
    return h.agent.post(
        f"{base}/operations/{operation}",
        json=arguments,
        headers={"Authorization": f"Bearer {token}"},
    )


def _connection(h: ChatHarness) -> str:
    return h.repository.list_connections("learn")[0].connection_id


def test_a_limit_the_user_asked_for_is_shown_approved_and_held_on_every_call(
    tmp_path: Path,
) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        proposal = h.propose(
            readOnly=True,
            alsoAllow=["create_issue"],
            limitArguments=[ENG],
            userWords="Connect the tracker, read only and creating issues, only project ENG.",
        )
        link = proposal["reviewLink"]
        page = h.review(link)

        # the owner sees the limit on each operation that takes a project (search_notes does not)
        assert page.count("Limited by you to: <b>project</b> ENG") == 3
        assert "is not limited on" not in page
        assert "Could not be matched" not in page

        assert h.approve(link, page).status_code == 302
        address, token = connector_from(h.get(link).text)

        for operation in ("list_issues", "get_issue", "create_issue"):
            assert _call(h, address, token, operation, project="ENG").status_code == 200
            outside = _call(h, address, token, operation, project="OPS")
            assert outside.status_code == 400
            assert "limited by the owner to ENG" in outside.json()["message"]
            assert _call(h, address, token, operation).status_code == 400  # no way round it
        assert [(name, args["project"]) for name, args, _ in h.tools.calls] == [
            ("list_issues", "ENG"),
            ("get_issue", "ENG"),
            ("create_issue", "ENG"),
        ]

        # what Muse reads says so, and so does what the agent can ask about
        spec = h.agent.get(_path(address)).json()
        operation = spec["paths"]["/operations/list_issues"]
        schema = operation["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert "project" in schema["required"]
        assert "Limits set by the owner: project must be ENG." in operation["post"]["description"]
        listed = h.ask("list_connections").json()["structuredContent"]["connections"]
        limits = {op["id"]: op.get("limits") for op in listed[0]["operations"]}
        assert limits == {
            "list_issues": {"project": "ENG"},
            "get_issue": {"project": "ENG"},
            "create_issue": {"project": "ENG"},
            "search_notes": None,  # takes no project
        }

        # the approved limit is part of what the audit trail records
        assert h.repository.verify_admin_audit_chain().ok
        digest_row = h.rows("SELECT request_json FROM setup_proposals")[0][0]
        assert '"limitArguments":[{"allowedValues":["ENG"]' in digest_row.replace(" ", "")


def test_a_limit_names_the_operation_or_every_operation_that_takes_the_argument(
    tmp_path: Path,
) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        one = {"operation": "list_issues", "argument": "project", "allowedValues": ["ENG", "OPS"]}
        link = h.propose(readOnly=True, limitArguments=[one])["reviewLink"]
        page = h.review(link)

        assert "<b>project</b> one of ENG, OPS" in page
        # the others take project too, and the owner is told they are not limited
        assert (
            "get_issue takes project, which is limited on list_issues but is not limited on" in page
        )


def test_a_limit_that_cannot_be_applied_is_said_not_dropped_in_silence(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        missing_operation = {"operation": "nope", "argument": "project", "allowedValues": ["ENG"]}
        missing_argument = {"operation": "search_notes", "argument": "project", "pattern": "E.*"}
        nowhere = {"operation": "*", "argument": "colour", "allowedValues": ["red"]}
        link = h.propose(
            readOnly=True, limitArguments=[missing_operation, missing_argument, nowhere]
        )["reviewLink"]
        page = h.review(link)

        assert "Could not be matched" in page
        assert "the server does not offer that operation" in page
        assert "the operation has no such argument" in page
        assert "no operation has that argument" in page
        assert "Limited by you to" not in page


def test_an_operation_the_owner_adds_on_the_page_keeps_a_limit_that_covers_everything(
    tmp_path: Path,
) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        link = h.propose(readOnly=True, limitArguments=[ENG])["reviewLink"]
        page = h.review(link)
        assert "tool:create_issue" not in ticked(page)

        # the owner ticks create_issue, which nobody asked for
        extra = [*ticked(page), "tool:create_issue"]
        decided = h.post(
            f"{_path(link)}/decide", action="approve", digest=digest_of(page), chosen=extra
        )
        assert decided.status_code == 302
        address, token = connector_from(h.get(link).text)

        assert _call(h, address, token, "create_issue", project="ENG").status_code == 200
        assert _call(h, address, token, "create_issue", project="OPS").status_code == 400


def test_the_agent_can_add_a_limit_at_once_and_it_can_only_ever_narrow(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        _, _, address, token = _connect(h, readOnly=True)
        connection = _connection(h)
        assert _call(h, address, token, "list_issues", project="HR").status_code == 200

        both = {"operation": "list_issues", "argument": "project", "allowedValues": ["ENG", "OPS"]}
        added = h.ask("limit_arguments", {"connectionId": connection, "limitArguments": [both]})
        assert added.status_code == 200, added.text
        before = h.repository.get_active_catalog("learn").catalog_version
        assert _call(h, address, token, "list_issues", project="HR").status_code == 400
        assert _call(h, address, token, "list_issues", project="OPS").status_code == 200
        assert _call(h, address, token, "get_issue", project="HR").status_code == 200  # not named

        # the same again changes nothing and publishes nothing
        again = h.ask("limit_arguments", {"connectionId": connection, "limitArguments": [both]})
        assert again.status_code == 200
        assert "already in place" in again.json()["structuredContent"]["message"]
        assert h.repository.get_active_catalog("learn").catalog_version == before

        # fewer values is narrower; more or different values is not allowed without the owner
        only = {"operation": "list_issues", "argument": "project", "allowedValues": ["ENG"]}
        assert (
            h.ask("limit_arguments", {"connectionId": connection, "limitArguments": [only]})
        ).status_code == 200
        assert _call(h, address, token, "list_issues", project="OPS").status_code == 400
        for wider in (["ENG", "OPS"], ["HR"]):
            body = {"operation": "list_issues", "argument": "project", "allowedValues": wider}
            refused = h.ask(
                "limit_arguments", {"connectionId": connection, "limitArguments": [body]}
            )
            assert refused.status_code == 409 and "propose_change" in refused.json()["message"]
        pattern = {"operation": "list_issues", "argument": "project", "pattern": "[A-Z]+"}
        assert (
            h.ask("limit_arguments", {"connectionId": connection, "limitArguments": [pattern]})
        ).status_code == 409
        assert _call(h, address, token, "list_issues", project="ENG").status_code == 200
        assert _call(h, address, token, "list_issues", project="OPS").status_code == 400

        # a name that is not exposed is an error that lists what is
        nowhere = {"operation": "create_issue", "argument": "project", "allowedValues": ["ENG"]}
        unknown = h.ask(
            "limit_arguments", {"connectionId": connection, "limitArguments": [nowhere]}
        )
        assert unknown.status_code == 400 and "list_issues" in unknown.json()["message"]

        # recorded like any other narrowing, on an unbroken chain
        assert h.repository.verify_admin_audit_chain().ok
        audit = h.rows("SELECT action FROM admin_audit WHERE action = 'setup.catalog.limited'")
        assert len(audit) == 2
        setup_id = h.repository.list_connections("adapter-setup")[0].connection_id
        on_setup = h.ask("limit_arguments", {"connectionId": setup_id, "limitArguments": [ENG]})
        assert on_setup.status_code == 409


def test_a_malformed_limit_is_refused_before_anything_is_opened(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        both = {**ENG, "pattern": "ENG"}
        neither = {"operation": "*", "argument": "project"}
        runaway = {"operation": "*", "argument": "project", "pattern": "(a+)+$"}
        crowded = {**ENG, "allowedValues": [str(n) for n in range(21)]}
        stray = {**ENG, "secret": "x"}
        for bad in (both, neither, runaway, crowded, stray):
            response = h.ask(
                "propose_connection",
                {"server": "learn", "userWords": "x", "readOnly": True, "limitArguments": [bad]},
            )
            assert response.status_code == 400, (bad, response.text)
        assert [s.server_id for s in h.repository.list_servers()] == ["adapter-setup"]


def test_changing_a_limit_needs_the_owner_and_a_new_operation_is_told_it_is_unlimited(
    tmp_path: Path,
) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        _, _, address, token = _connect(h, readOnly=True, limitArguments=[ENG])
        connection = _connection(h)
        assert _call(h, address, token, "get_issue", project="OPS").status_code == 400

        # more access with a limit: the owner is told the new operation does not carry it
        asked = h.ask(
            "propose_change",
            {
                "connectionId": connection,
                "userWords": "also let it create issues",
                "alsoAllow": ["create_issue"],
            },
        )
        link = asked.json()["structuredContent"]["reviewLink"]
        page = h.review(link)
        assert "create_issue takes project, which is limited on" in page
        assert h.approve(link, page).status_code == 302
        assert _call(h, address, token, "create_issue", project="OPS").status_code == 200
        assert _call(h, address, token, "get_issue", project="OPS").status_code == 400

        # a looser limit is a change the owner approves
        looser = {"operation": "*", "argument": "project", "allowedValues": ["ENG", "OPS"]}
        widen = h.ask(
            "propose_change",
            {"connectionId": connection, "userWords": "ENG and OPS", "limitArguments": [looser]},
        )
        link = widen.json()["structuredContent"]["reviewLink"]
        assert _call(h, address, token, "get_issue", project="OPS").status_code == 400  # not yet
        page = h.review(link)
        assert "<b>project</b> one of ENG, OPS" in page
        assert h.approve(link, page).status_code == 302
        assert _call(h, address, token, "get_issue", project="OPS").status_code == 200
        assert _call(h, address, token, "get_issue", project="HR").status_code == 400


def test_a_redefined_operation_keeps_its_limit_while_the_argument_exists(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        _, _, _, _ = _connect(h, readOnly=True, limitArguments=[ENG])
        connection = _connection(h)

        manifest: dict[str, Any] = projects_manifest()
        for primitive in manifest["primitives"]:
            if primitive["name"] == "list_issues":
                primitive["description"] = "A new description, so the definition differs."
            if primitive["name"] == "get_issue":
                primitive["inputSchema"] = {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                }
        h.discovery.manifest = manifest

        asked = h.ask(
            "propose_change",
            {"connectionId": connection, "userWords": "refresh", "timeoutSeconds": 20},
        )
        page = h.review(asked.json()["structuredContent"]["reviewLink"])

        assert "The server changed this since it was approved." in page
        assert page.count("Limited by you to: <b>project</b> ENG") >= 1  # list_issues keeps it
        assert "the limit on project (ENG) is gone because the server&#x27;s new definition" in page
