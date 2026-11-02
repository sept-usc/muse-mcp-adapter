"""Chat-driven setup end to end: Muse asks, the owner reviews, and only then is anything exposed."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tests.fixtures.chat_setup import (
    ChatHarness,
    chat_harness,
    connector_from,
    digest_of,
    run_cli,
)


def _path(address: str) -> str:
    return re.sub(r"^https?://[^/]+", "", address)


def _call(h: ChatHarness, address: str, token: str, operation: str, body: dict[str, str]) -> int:
    base = _path(address).removesuffix("/openapi.json")
    return h.agent.post(
        f"{base}/operations/{operation}", json=body, headers={"Authorization": f"Bearer {token}"}
    ).status_code


def _offered(h: ChatHarness, address: str) -> list[str]:
    spec: dict[str, Any] = h.agent.get(_path(address)).json()
    return sorted(path.rsplit("/", 1)[1] for path in spec["paths"] if "/operations/" in path)


def test_a_proposal_exposes_nothing_until_the_owner_approves(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose(readOnly=True, alsoAllow=["create_issue"])

        assert proposal["state"] == "waiting_for_owner"
        assert proposal["reviewLink"].startswith("https://adapter.example.com/review/")
        assert "link" in proposal["nextStep"].lower()
        # nothing was registered, contacted or exposed by asking
        assert [s.server_id for s in h.repository.list_servers()] == ["adapter-setup"]
        assert h.discovery.credentials == []


def test_the_full_loop_from_a_sentence_to_enforced_calls_and_a_traceable_record(
    tmp_path: Path,
) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose(
            readOnly=True,
            alsoAllow=["create_issue"],
            userWords="Connect the tracker, read only, and let it create issues.",
        )
        link = proposal["reviewLink"]

        page = h.review(link)
        # the owner sees what would become available, with what each can do
        for name in ("list_issues", "get_issue", "create_issue"):
            assert f"<b>{name}</b>" in page
        assert "Available but not asked for: 0" in page
        assert "Connect the tracker, read only" in page  # the words, labelled as relayed

        assert h.approve(link, page).status_code == 302
        done = h.get(link)
        assert done.status_code == 200
        address, token = connector_from(done.text)

        # the agent is told it is ready, and what to do next
        status = h.status(proposal["proposalId"])
        assert status["state"] == "ready"
        assert status["connector"]["apiDescriptionUrl"] == address
        assert {item["id"] for item in status["connector"]["operations"]} == {
            "list_issues",
            "get_issue",
            "create_issue",
        }

        # what Muse reads lists exactly what was approved
        assert _offered(h, address) == ["create_issue", "get_issue", "list_issues"]

        # and the runtime enforces it: allowed works, anything else does not exist
        assert _call(h, address, token, "list_issues", {"id": "1"}) == 200
        assert _call(h, address, token, "create_issue", {"id": "2"}) == 200
        assert _call(h, address, token, "delete_issue", {"id": "3"}) == 404
        assert _call(h, address, token, "search_notes", {"id": "4"}) == 404
        assert [call[0] for call in h.tools.calls] == ["list_issues", "create_issue"]

        # every step is on a hash chain, and a call traces back to the owner's words
        assert h.repository.verify_admin_audit_chain().ok
        request_id = h.repository.list_runtime_audit()[-1]["request_id"]
        trace = run_cli(h.database, h.config_path, "explain-call", "--request-id", str(request_id))
        assert trace["proposal"]["userWords"].startswith("Connect the tracker, read only")
        assert trace["proposal"]["decision"]["method"] == "owner-code"


def test_declining_leaves_nothing_behind(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose(readOnly=True)
        link = proposal["reviewLink"]
        page = h.review(link)

        declined = h.post(f"{_path(link)}/decide", action="decline", digest=digest_of(page))
        assert declined.status_code == 302

        assert h.status(proposal["proposalId"])["state"] == "declined"
        assert h.get(link).status_code == 410
        assert h.repository.get_server("learn").enabled is False
        assert all(c.revoked_at is not None for c in h.repository.list_connections("learn"))
