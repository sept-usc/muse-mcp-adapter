"""The ways a request, a page or a step can go wrong, and what the owner and the agent are told."""

from __future__ import annotations

import contextlib
import re
import sqlite3
from pathlib import Path
from typing import Any, cast

from muse_mcp_adapter.application.control_plane_service import ControlPlaneService
from muse_mcp_adapter.connections import ConnectionService
from muse_mcp_adapter.discovery.client import ManifestDiscoveryClient
from tests.fixtures.chat_setup import (
    ChatHarness,
    chat_harness,
    connector_from,
    run_cli,
    tracker_manifest,
)


def _path(link: str) -> str:
    return re.sub(r"^https?://[^/]+", "", link)


def _sql(h: ChatHarness, statement: str) -> None:
    with contextlib.closing(sqlite3.connect(h.database)) as raw:
        raw.execute(statement)
        raw.commit()


def _connect(h: ChatHarness, **fields: object) -> str:
    link = h.propose(**fields)["reviewLink"]
    assert h.approve(link, h.review(link)).status_code == 302
    return str(connector_from(h.get(link).text)[0])


def test_the_agent_is_told_plainly_when_a_request_cannot_be_met(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        unknown = h.ask("propose_connection", {"server": "nosuch", "userWords": "x"})
        assert unknown.status_code == 400 and "find_server" in unknown.json()["message"]
        insecure = h.ask(
            "propose_connection", {"endpoint": "http://plain.example.test/mcp", "userWords": "x"}
        )
        assert insecure.status_code == 400 and "HTTPS" in insecure.json()["message"]

        _connect(h, readOnly=True)
        again = h.ask("propose_connection", {"server": "learn", "userWords": "x"})
        assert again.status_code == 409 and "propose_change" in again.json()["message"]

        gone = h.repository.list_connections("learn")[0]
        h.ask("remove_connection", {"connectionId": gone.connection_id})
        late = h.ask(
            "propose_change",
            {"connectionId": gone.connection_id, "userWords": "x", "readOnly": True},
        )
        assert late.status_code == 404
        setup = h.repository.list_connections("adapter-setup")[0]
        own = h.ask(
            "propose_change",
            {"connectionId": setup.connection_id, "userWords": "x", "readOnly": True},
        )
        assert own.status_code == 409


def test_server_ids_made_from_addresses_are_valid_and_never_collide(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        for endpoint in (
            "https://1.example.test/mcp",
            "https://ab/mcp",
            "https://mcp.example.test/one",
        ):
            h.ask("propose_connection", {"endpoint": endpoint, "userWords": endpoint})
        ids = [item["serverId"] for item in run_cli(h.database, h.config_path, "proposals")]
        assert sorted(ids) == ["ab-mcp", "mcp-example-test", "s-1-example-test"]
        assert all(re.fullmatch(r"[a-z][a-z0-9-]{2,63}", item) for item in ids)

        # a different address on the same host does not take over the name
        control = ControlPlaneService(h.repository)
        control.register_server(
            server_id="other-example-test",
            display_name="Other",
            endpoint="https://other.example.test/first",
            auth_profile="none",
        )
        h.ask(
            "propose_connection",
            {"endpoint": "https://other.example.test/second", "userWords": "y"},
        )
        listed = run_cli(h.database, h.config_path, "proposals")
        assert any(
            re.fullmatch(r"other-example-test-[0-9a-f]{6}", str(i["serverId"])) for i in listed
        )


def test_a_connection_made_at_the_command_line_can_be_listed_and_explained(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        control = ControlPlaneService(h.repository)
        control.register_server(
            server_id="manual",
            display_name="Manual",
            endpoint="https://m.example.test/mcp",
            auth_profile="none",
        )
        control.discover("manual", ManifestDiscoveryClient(tracker_manifest()))
        control.publish_catalog(
            server_id="manual",
            catalog_version="1.0.0",
            policies=[
                {
                    "primitiveKey": "tool:list_issues",
                    "allowed": True,
                    "publicOperationId": "list_issues",
                    "classification": "read",
                    "maxResultBytes": 1000,
                }
            ],
        )
        control.activate_catalog("manual", "1.0.0")
        linked = ConnectionService(h.repository).link(
            server_id="manual", label="Manual", credential_kind="none"
        )

        listed = h.ask("list_connections").json()["structuredContent"]
        assert [c["serverId"] for c in listed["connections"]] == ["manual"]
        explained = h.ask("explain_connection", {"connectionId": linked.record.connection_id})
        assert "no recorded request" in explained.json()["structuredContent"]["message"]

        h.ask(
            "turn_off_operations",
            {"connectionId": linked.record.connection_id, "operations": ["list_issues"]},
        )
        again = h.ask("explain_connection", {"connectionId": linked.record.connection_id})
        assert "adapter itself" in again.json()["structuredContent"]["message"]


def test_the_owner_is_told_when_a_step_comes_out_of_order_or_input_is_missing(
    tmp_path: Path,
) -> None:
    with chat_harness(tmp_path) as h:
        link = h.propose(readOnly=True)["reviewLink"]
        h.unlock(link)
        h.get(link)
        too_early = h.post(f"{_path(link)}/discover")
        assert too_early.status_code == 200 and "not ready" in too_early.text
        unsure = h.post(f"{_path(link)}/start", plan="bogus")
        assert unsure.status_code in (200, 302)

    (tmp_path / "b").mkdir()
    with chat_harness(tmp_path / "b") as h:
        link = h.propose(server="github", readOnly=True)["reviewLink"]
        h.unlock(link)
        h.get(link)
        missing = h.post(f"{_path(link)}/start", secret="")
        assert missing.status_code == 200 and "Enter the token" in missing.text
        odd = h.post(f"{_path(link)}/start", secret="line one\nline two")
        assert odd.status_code == 200 and "does not look like a token" in odd.text
        assert h.repository.list_connections("github") == ()  # nothing was created


def test_a_server_registered_elsewhere_is_not_taken_over(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        ControlPlaneService(h.repository).register_server(
            server_id="github",
            display_name="Not the real one",
            endpoint="https://not-github.example.test/mcp",
            auth_profile="none",
        )
        link = h.propose(server="github", readOnly=True)["reviewLink"]
        h.unlock(link)
        h.get(link)
        refused = h.post(f"{_path(link)}/start", secret="ghp_whatever")
        assert refused.status_code == 200 and "another address" in refused.text
        assert _secrets(h) == 0


def _secrets(h: ChatHarness) -> int:
    with contextlib.closing(sqlite3.connect(h.database)) as raw:
        return len(raw.execute("SELECT * FROM owner_secrets").fetchall())


def test_a_change_made_stale_by_a_narrowing_is_stopped_not_applied(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        _connect(h, readOnly=True)
        connection_id = h.repository.list_connections("learn")[0].connection_id
        asked = h.ask(
            "propose_change",
            {
                "connectionId": connection_id,
                "userWords": "add create",
                "alsoAllow": ["create_issue"],
            },
        ).json()["structuredContent"]
        link = asked["reviewLink"]
        page = h.review(link)

        # while the owner reviews, the live policy moves
        h.ask("turn_off_operations", {"connectionId": connection_id, "operations": ["get_issue"]})
        stopped = h.approve(link, page)
        assert stopped.status_code == 409 and "changed while you were reviewing" in stopped.text
        status = h.status(asked["proposalId"])
        assert status["state"] == "failed" and "review page" in status["message"]
        live = {
            item["id"]
            for item in h.ask("list_connections").json()["structuredContent"]["connections"][0][
                "operations"
            ]
        }
        assert live == {"list_issues"}  # the stale change did not bring anything back
        assert h.repository.verify_admin_audit_chain().ok


def test_without_an_owner_code_nothing_can_be_unlocked(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        link = h.propose(readOnly=True)["reviewLink"]
        _sql(h, "DELETE FROM owner_credential")
        h.get(link)
        response = h.post(f"{_path(link)}/unlock", code=h.owner_code)
        assert response.status_code == 409 and "No owner code" in response.text


def test_steps_out_of_order_after_the_end_do_nothing(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        link = h.propose(readOnly=True)["reviewLink"]
        page = h.review(link)
        assert h.approve(link, page).status_code == 302
        for step in ("start", "discover"):
            late = h.post(f"{_path(link)}/{step}")
            assert late.status_code == 200
        assert h.repository.verify_admin_audit_chain().ok


def test_a_review_link_is_shown_clear_of_clutter_when_it_is_unknown_or_ended(
    tmp_path: Path,
) -> None:
    with chat_harness(tmp_path) as h:
        assert h.get("/review/not-a-real-link").status_code == 404
        assert h.post("/review/not-a-real-link/unlock", code="x").status_code == 404
        proposal = h.propose(readOnly=True)
        _sql(
            h,
            "UPDATE setup_proposals SET state = 'failed', error = 'It broke.' "
            f"WHERE proposal_id = '{proposal['proposalId']}'",
        )
        stopped = h.get(proposal["reviewLink"])  # only the owner is told why
        assert "owner code" in stopped.text.lower() and "It broke." not in stopped.text
        h.unlock(proposal["reviewLink"])
        failed = h.get(proposal["reviewLink"])
        assert failed.status_code == 409 and "It broke." in failed.text
        too_big = h.browser.post(
            f"{_path(proposal['reviewLink'])}/unlock",
            content=b"x" * 200_000,
            headers={"content-type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert too_big.status_code in (404, 409, 413)


def test_the_setup_catalog_is_republished_when_the_code_changes(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        before = h.repository.get_active_catalog("adapter-setup")
        h.repository.activate_catalog(
            "adapter-setup", before.catalog_version, "2026-10-05T10:00:00Z"
        )
        names = {p.public_operation_id for p in before.primitives}
        assert names == {
            "how_setup_works",
            "find_server",
            "propose_connection",
            "propose_change",
            "get_proposal_status",
            "list_connections",
            "explain_connection",
            "turn_off_operations",
            "limit_arguments",
            "expire_access",
            "remove_connection",
        }
        by_name = {p.public_operation_id: p.classification for p in before.primitives}
        assert (
            by_name["propose_connection"] == "read"
        )  # it changes nothing until the owner approves
        assert (
            by_name["turn_off_operations"] == "write"
            and by_name["limit_arguments"] == "write"
            and by_name["expire_access"] == "write"
            and by_name["remove_connection"] == "sensitive_write"
        )


def test_what_muse_must_send_to_the_setup_connector_is_plain_json_schema(tmp_path: Path) -> None:
    """Nothing in the request bodies that a stricter OpenAPI reader could trip over: no empty
    required lists and no type unions (the shared response schemas are used by every connector)."""
    with chat_harness(tmp_path) as h:
        spec: Any = h.agent.get(f"/c/{h.setup_slug}/openapi.json").json()
        operations = {path: item for path, item in spec["paths"].items() if "/operations/" in path}
        assert len(operations) == 11

        def walk(node: object, where: str) -> None:
            if isinstance(node, dict):
                fields = cast(dict[str, Any], node)
                assert fields.get("required") != [], f"empty required list at {where}"
                assert not isinstance(fields.get("type"), list), f"type union at {where}"
                for key, value in fields.items():
                    walk(value, f"{where}/{key}")
            elif isinstance(node, list):
                for number, value in enumerate(cast(list[Any], node)):
                    walk(value, f"{where}[{number}]")

        for path, item in operations.items():
            body = item["post"].get("requestBody")
            if body is not None:
                assert body["content"]["application/json"]["schema"]["type"] == "object"
                walk(body, path)
