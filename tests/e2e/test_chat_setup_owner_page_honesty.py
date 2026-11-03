"""What the owner is told must match what the adapter can do and what was really asked."""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

from muse_mcp_adapter.data_plane.read_service import supports_template
from tests.fixtures.chat_setup import chat_harness, projects_manifest


def _path(address: str) -> str:
    return re.sub(r"^https?://[^/]+", "", address)


def _many_reads_and_two_templates() -> dict[str, object]:
    manifest = projects_manifest()
    extra: list[object] = [
        {
            "type": "tool",
            "name": f"read_{n}",
            "description": "reads",
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": True},
        }
        for n in range(4)
    ]
    extra.append(
        {"type": "resource_template", "name": "plain", "uriTemplate": "tracker://{project}/board"}
    )
    extra.append(
        {
            "type": "resource_template",
            "name": "rfc6570",
            "uriTemplate": "repo://{owner}/contents{/path*}",
        }
    )
    return {**manifest, "primitives": [*cast(list[object], manifest["primitives"]), *extra]}


def test_only_plain_templates_can_be_filled_in() -> None:
    assert supports_template("mcp://issues/{issueId}/document")
    assert supports_template("static://no-fields")
    assert not supports_template("repo://{owner}/{repo}/contents{/path*}")
    assert not supports_template("x://{a.b}")
    assert not supports_template("x://{a!r}")
    assert not supports_template("x://{unclosed")


def test_a_template_the_adapter_cannot_fill_in_is_not_offered_unasked_and_the_page_says_why(
    tmp_path: Path,
) -> None:
    with chat_harness(tmp_path, manifest=_many_reads_and_two_templates()) as h:
        link = h.propose(readOnly=True)["reviewLink"]
        page = h.review(link)

        assert "name=chosen value='resource_template:plain' checked" in page
        assert "name=chosen value='resource_template:rfc6570' checked" not in page
        assert "cannot fill in this address template yet" in page


def test_a_bare_read_only_request_says_what_it_really_means(tmp_path: Path) -> None:
    with chat_harness(tmp_path, manifest=_many_reads_and_two_templates()) as h:
        page = h.review(h.propose(readOnly=True)["reviewLink"])
        assert "every read operation the service offers counts as asked for" in page

    (tmp_path / "b").mkdir()
    with chat_harness(tmp_path / "b", manifest=projects_manifest()) as h:  # few operations: no note
        page = h.review(h.propose(readOnly=True)["reviewLink"])
        assert "counts as asked for" not in page


def test_a_service_that_is_not_an_mcp_server_gets_a_plain_sentence(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        link = h.propose(readOnly=True)["reviewLink"]
        h.discovery.failure = ExceptionGroup(
            "unhandled errors in a TaskGroup", [ConnectionError("no route")]
        )
        h.unlock(link)
        started = h.post(f"{_path(link)}/start", plan="none")

        assert "TaskGroup" not in started.text
        assert "did not answer as an MCP server (ConnectionError)" in started.text
