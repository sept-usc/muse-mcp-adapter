"""The last few paths: a resource under a limit, an unknown command, a second press on a page."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from muse_mcp_adapter.chat_setup import commands
from muse_mcp_adapter.chat_setup.compile import compile_proposal
from muse_mcp_adapter.chat_setup.models import SetupRequest
from tests.fixtures.chat_setup import chat_harness, projects_manifest
from tests.unit.test_chat_setup_compile import PRIMITIVES


def test_a_resource_takes_no_arguments_so_a_limit_does_not_note_it() -> None:
    request = SetupRequest.parse(
        {
            "endpoint": "https://t.example/mcp",
            "userWords": "w",
            "alsoAllow": ["readme"],
            "limitArguments": [{"operation": "*", "argument": "project", "allowedValues": ["ENG"]}],
        },
        change=False,
    )
    computed = compile_proposal(
        primitives=PRIMITIVES, request=request, current=None, current_digests=None,
        snapshot_digest="sha256:x", base_version=None,
    )  # fmt: skip
    notes = " ".join(computed.unenforced)
    assert "readme" not in notes  # it takes no argument, so there is nothing to be held to


def test_a_command_nobody_handles_is_a_programming_error_not_a_silent_success() -> None:
    args: Any = argparse.Namespace(command="not-a-command", database=":memory:")
    with pytest.raises((AssertionError, Exception)):
        commands.run(args, None, None, None)  # type: ignore[arg-type]


def test_a_second_press_that_finds_the_proposal_already_moved_on_just_goes_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        proposal = h.ask(
            "propose_connection",
            {"endpoint": "https://tracker.example/mcp", "userWords": "w", "readOnly": True},
        ).json()["structuredContent"]
        link = proposal["reviewLink"]
        path = link.removeprefix("https://adapter.example.com")
        assert h.unlock(link).status_code == 302

        # another press has already taken it past the point of looking at the service
        def moved_on(*args: object, **kwargs: object) -> None:
            return None

        monkeypatch.setattr(type(h.service), "start", moved_on)
        again = h.post(f"{path}/start", plan="none")
        assert again.status_code == 302 and again.headers["location"] == path
        assert h.status(proposal["proposalId"])["state"] == "waiting_for_owner"


def test_the_arguments_an_operation_takes_are_read_from_its_own_kind() -> None:
    from muse_mcp_adapter.chat_setup.compile import (
        _arguments_taken,  # pyright: ignore[reportPrivateUsage]
    )

    tool: dict[str, Any] = {
        "type": "tool",
        "name": "t",
        "inputSchema": {"properties": {"project": {}, "id": {}}},
    }
    assert _arguments_taken(tool) == {"project", "id"}
    assert _arguments_taken({"type": "resource", "name": "readme"}) == set()
    assert _arguments_taken({"type": "prompt", "arguments": [{"name": "topic"}, "odd"]}) == {
        "topic"
    }
    assert _arguments_taken({"type": "prompt", "arguments": "nope"}) == set()
    assert _arguments_taken({"type": "resource_template", "uriTemplate": "x://{a,b*}/{+c}"}) == {
        "a",
        "b",
        "c",
    }
