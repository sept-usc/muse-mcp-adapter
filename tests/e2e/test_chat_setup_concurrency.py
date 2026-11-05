"""Changes made at the same moment all take effect: none is lost while its caller hears yes."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from muse_mcp_adapter.domain.models import PublishedCatalog
from tests.fixtures.chat_setup import chat_harness, projects_manifest

LIMITS = [
    {"operation": "list_issues", "argument": "project", "allowedValues": ["ENG"]},
    {"operation": "list_issues", "argument": "query", "pattern": ".{1,50}"},
    {"operation": "get_issue", "argument": "project", "allowedValues": ["ENG"]},
    {"operation": "get_issue", "argument": "id", "pattern": "[0-9]+"},
]


def test_limits_asked_for_at_the_same_moment_all_take_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with chat_harness(tmp_path, manifest=projects_manifest()) as h:
        link = h.propose(readOnly=True)["reviewLink"]
        assert h.approve(link, h.review(link)).status_code == 302
        connection = h.repository.list_connections("learn")[0].connection_id

        # make every reader of the live catalog slow, so all of them start from the same one
        original = h.repository.get_active_catalog

        def slow(server_id: str) -> PublishedCatalog:
            catalog = original(server_id)
            time.sleep(0.05)
            return catalog

        monkeypatch.setattr(h.repository, "get_active_catalog", slow)
        barrier = threading.Barrier(len(LIMITS))

        def ask(limit: dict[str, Any]) -> int:
            barrier.wait()
            body = {"connectionId": connection, "limitArguments": [limit]}
            return int(h.ask("limit_arguments", body).status_code)

        with ThreadPoolExecutor(max_workers=len(LIMITS)) as pool:
            statuses = list(pool.map(ask, LIMITS))
        monkeypatch.undo()

        # whatever was told "in place" really is in place
        listed: Any = h.ask("explain_connection", {"connectionId": connection}).json()
        live: dict[str, dict[str, str]] = {
            str(op["id"]): dict(op.get("limits") or {})
            for op in listed["structuredContent"]["operations"]
        }
        asked = {(item["operation"], item["argument"]) for item in LIMITS}
        said_yes = {
            (item["operation"], item["argument"])
            for item, status in zip(LIMITS, statuses, strict=True)
            if status == 200
        }
        held = {(op, arg) for op, limits in live.items() for arg in limits}
        assert said_yes <= held, f"told yes but lost: {sorted(said_yes - held)}"
        assert held <= asked
        assert statuses.count(200) >= 1
