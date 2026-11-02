"""Temporary access: "connect it for two hours" ends by itself, and only the owner can extend it."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from muse_mcp_adapter.domain.models import OperationRecord
from tests.fixtures.chat_setup import (
    ChatHarness,
    chat_harness,
    connector_from,
    projects_manifest,
)

HOUR = 3600
START = "2026-10-05T10:00:00Z"


def _path(address: str) -> str:
    return re.sub(r"^https?://[^/]+", "", address)


def _connect(h: ChatHarness, **fields: object) -> tuple[dict[str, Any], str, str, str]:
    proposal = h.propose(**fields)
    link = proposal["reviewLink"]
    page = h.review(link)
    assert h.approve(link, page).status_code == 302
    address, token = connector_from(h.get(link).text)
    return proposal, link, address, token


def _call(h: ChatHarness, address: str, token: str, operation: str = "list_issues") -> Any:
    base = _path(address).removesuffix("/openapi.json")
    return h.agent.post(
        f"{base}/operations/{operation}",
        json={"project": "ENG"},
        headers={"Authorization": f"Bearer {token}"},
    )


def _ends(h: ChatHarness) -> set[object]:
    return {p.policy.get("expiresAt") for p in h.repository.get_active_catalog("learn").primitives}


def _connection(h: ChatHarness) -> str:
    return h.repository.list_connections("learn")[0].connection_id


def test_access_asked_for_a_time_ends_by_itself_counted_from_the_approval(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        proposal = h.propose(
            readOnly=True, expiresInHours=2, userWords="Connect the tracker for two hours."
        )
        link = proposal["reviewLink"]
        page = h.review(link)
        assert "Access is temporary: it ends 2 hours after you approve" in page
        assert "the time shown above" in page

        h.clock.advance(600)  # the owner takes ten minutes to decide: the two hours start then
        assert h.approve(link, page).status_code == 302
        done = h.get(link).text
        address, token = connector_from(done)
        end = "2026-10-05T12:10:00Z"
        assert _ends(h) == {end}
        assert f"Access ends at {end}" in done

        # until then it works, and what Muse reads says when it ends
        assert _call(h, address, token).status_code == 200
        spec = h.agent.get(_path(address)).json()
        described = spec["paths"]["/operations/list_issues"]["post"]["description"]
        assert f"The owner set this access to end at {end}." in described
        h.clock.advance(2 * HOUR - 1)
        assert _call(h, address, token).status_code == 200

        # at the end it is refused, says why, and drops out of what Muse reads
        h.clock.advance(1)
        refused = _call(h, address, token)
        assert refused.status_code == 403
        assert f"ended at {end}" in refused.json()["message"]
        assert "Ask the owner to renew it" in refused.json()["message"]
        assert "/operations/list_issues" not in h.agent.get(_path(address)).json()["paths"]
        assert [name for name, _, _ in h.tools.calls] == ["list_issues", "list_issues"]  # not 3

        # the failed call is on the record, and the agent can tell the user what happened
        last = h.repository.list_runtime_audit()[-1]
        assert last["outcome"] == "failed" and last["error_code"] == "AUTHORIZATION_FAILED"
        operations = h.ask("list_connections").json()["structuredContent"]["connections"][0]
        assert all(op["accessEnded"] is True for op in operations["operations"])
        assert {op["accessEndsAt"] for op in operations["operations"]} == {end}
        assert h.repository.verify_admin_audit_chain().ok


def test_a_resumed_call_is_refused_after_the_end_too(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        _, _, address, token = _connect(
            h, readOnly=True, alsoAllow=["create_issue"], expiresInHours=1
        )
        slug = _path(address).removesuffix("/openapi.json")
        h.repository.create_operation(
            OperationRecord(
                operation_id="op-waiting",
                request_id="request-waiting",
                server_id="learn",
                catalog_version=h.repository.get_active_catalog("learn").catalog_version,
                public_operation_id="create_issue",
                local_identity_id=h.repository.list_connections("learn")[0].identity_id,
                connection_id=_connection(h),
                classification="write",
                status="INPUT_REQUIRED",
                idempotency_key=None,
                request_digest="digest",
                approval_id=None,
                created_at=START,
                updated_at=START,
                result=None,
                error_code=None,
                error_message=None,
                retryable=False,
            )
        )
        h.clock.advance(HOUR)
        resumed = h.agent.post(
            f"{slug}/executions/op-waiting/resume",
            json={"arguments": {"project": "ENG"}, "inputs": {}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resumed.status_code == 403 and "ended at" in resumed.json()["message"]
        assert h.tools.calls == []


def test_adding_to_a_connection_does_not_give_the_new_operation_a_longer_life(
    tmp_path: Path,
) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        _, _, address, token = _connect(h, readOnly=True, expiresInHours=3)
        end = "2026-10-05T13:00:00Z"
        h.clock.advance(1800)

        asked = h.ask(
            "propose_change",
            {
                "connectionId": _connection(h),
                "userWords": "also let it create issues",
                "alsoAllow": ["create_issue"],
            },
        )
        link = asked.json()["structuredContent"]["reviewLink"]
        page = h.review(link)
        assert f"Access ends at {end}, as it did before" in page
        assert h.approve(link, page).status_code == 302
        assert _ends(h) == {end}  # old and new end together

        assert _call(h, address, token, "create_issue").status_code == 200
        h.clock.advance(3 * HOUR)
        assert _call(h, address, token, "create_issue").status_code == 403
        assert _call(h, address, token, "list_issues").status_code == 403


def test_extending_access_is_a_change_only_the_owner_can_approve(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        _, _, address, token = _connect(h, readOnly=True, expiresInHours=1)
        h.clock.advance(1800)

        asked = h.ask(
            "propose_change",
            {"connectionId": _connection(h), "userWords": "five more hours", "expiresInHours": 5},
        )
        link = asked.json()["structuredContent"]["reviewLink"]
        assert _ends(h) == {"2026-10-05T11:00:00Z"}  # asking changes nothing
        page = h.review(link)
        assert "Access is temporary: it ends 5 hours after you approve" in page

        assert h.approve(link, page).status_code == 302
        assert _ends(h) == {"2026-10-05T15:30:00Z"}  # every operation, counted from the approval
        h.clock.advance(2 * HOUR)  # past the old end
        assert _call(h, address, token).status_code == 200
        h.clock.advance(4 * HOUR)  # past the new one
        assert _call(h, address, token).status_code == 403


def test_the_agent_can_end_access_sooner_at_once_and_never_later(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        _, _, address, token = _connect(h, readOnly=True)
        connection = _connection(h)
        assert _ends(h) == {None}  # no end to begin with

        first = h.ask("expire_access", {"connectionId": connection, "hours": 3})
        assert first.status_code == 200, first.text
        assert _ends(h) == {"2026-10-05T13:00:00Z"}
        version = h.repository.get_active_catalog("learn").catalog_version

        longer = h.ask("expire_access", {"connectionId": connection, "hours": 5})
        assert longer.status_code == 200
        assert "already ends sooner" in longer.json()["structuredContent"]["message"]
        assert h.repository.get_active_catalog("learn").catalog_version == version

        shorter = h.ask("expire_access", {"connectionId": connection, "hours": 1})
        assert shorter.status_code == 200 and "ends at 2026-10-05T11:00:00Z" in shorter.text
        assert _ends(h) == {"2026-10-05T11:00:00Z"}

        assert _call(h, address, token).status_code == 200
        h.clock.advance(HOUR)
        assert _call(h, address, token).status_code == 403

        # nothing here is outside the rules for narrowing, and it is all on the record
        origins = h.rows("SELECT origin FROM catalog_provenance WHERE origin = 'narrowing'")
        assert len(origins) == 2
        assert len(h.rows("SELECT 1 FROM admin_audit WHERE action = 'setup.catalog.expiry'")) == 2
        assert h.repository.verify_admin_audit_chain().ok


def test_an_end_is_refused_when_it_is_not_a_sensible_number_of_hours(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        _connect(h, readOnly=True)
        connection = _connection(h)
        for hours in (0, 721, "2", 1.5, None):
            body = {"connectionId": connection, "hours": hours}
            assert h.ask("expire_access", body).status_code == 400, hours
        for hours in (0, 721, "2"):
            response = h.ask(
                "propose_connection",
                {"server": "learn", "userWords": "x", "readOnly": True, "expiresInHours": hours},
            )
            assert response.status_code == 400, hours
        setup = h.repository.list_connections("adapter-setup")[0].connection_id
        assert h.ask("expire_access", {"connectionId": setup, "hours": 1}).status_code == 409
        assert _ends(h) == {None}


def test_a_connection_made_without_a_time_never_ends(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        _, _, address, token = _connect(h, readOnly=True)
        h.clock.advance(365 * 24 * HOUR)
        assert _call(h, address, token).status_code == 200
        assert "end at" not in str(h.agent.get(_path(address)).json())


def test_the_owner_is_warned_when_a_change_would_add_to_access_that_already_ended(
    tmp_path: Path,
) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        _connect(h, readOnly=True, expiresInHours=1)
        h.clock.advance(2 * HOUR)

        plain = h.ask(
            "propose_change",
            {
                "connectionId": _connection(h),
                "userWords": "also let it create issues",
                "alsoAllow": ["create_issue"],
            },
        )
        page = h.review(plain.json()["structuredContent"]["reviewLink"])
        assert "Access to this connection ended at 2026-10-05T11:00:00Z" in page
        assert "ask Muse for a time" in page

        renewed = h.ask(
            "propose_change",
            {
                "connectionId": _connection(h),
                "userWords": "again, for a while",
                "expiresInHours": 4,
            },
        )
        again = h.review(renewed.json()["structuredContent"]["reviewLink"])
        assert "Access is temporary: it ends 4 hours after you approve" in again
        assert "ended at" not in again
