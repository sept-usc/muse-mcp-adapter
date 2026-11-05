"""The tools around chat setup, on the days they are used in odd circumstances.

A call recorded before catalogs were tracked, two people closing the same proposal at once, a
setup catalog that changes between versions, an error wrapped in several others, and an owner who
presses the same button again after the server was down.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from muse_mcp_adapter.application.control_plane_service import ControlPlaneService
from muse_mcp_adapter.chat_setup import catalog as setup_catalog
from muse_mcp_adapter.chat_setup.commands import (
    _narrowing_entry,  # pyright: ignore[reportPrivateUsage]
)
from muse_mcp_adapter.chat_setup.store import SetupStore
from muse_mcp_adapter.chat_setup.web import _leaf  # pyright: ignore[reportPrivateUsage]
from muse_mcp_adapter.domain.errors import ConflictError
from tests.fixtures.chat_setup import chat_harness, run_cli


def _path(link_or_path: str) -> str:
    return re.sub(r"^https?://[^/]+", "", link_or_path)


# --- tracing a call ----------------------------------------------------------------------


def test_a_call_recorded_without_a_catalog_is_shown_as_far_as_it_goes(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        connection = h.repository.list_connections("adapter-setup")[0].connection_id
        h.repository.record_runtime_audit(
            occurred_at="2026-10-05T10:00:00Z",
            request_id="r-before-catalogs",
            server_id="adapter-setup",
            public_operation_id="find_server",
            local_identity_id="owner",
            connection_id=connection,
            catalog_version=None,
            classification=None,
            approval_id=None,
            outcome="completed",
            error_code=None,
            duration_ms=3,
        )

        shown = run_cli(
            h.database, h.config_path, "explain-call", "--request-id", "r-before-catalogs"
        )

        assert list(shown) == ["call"]
        assert shown["call"]["public_operation_id"] == "find_server"
        assert shown["call"]["catalog_version"] is None


# --- finding the agent's own entry for a narrowing ---------------------------------------


def _audit(h: Any, action: str, version: str) -> None:
    h.repository.record_admin_audit(
        occurred_at="2026-10-05T10:00:00Z",
        actor="chat-setup:agent",
        action=action,
        server_id="sample",
        data={"version": version},
    )


def test_the_agents_own_entry_is_preferred_to_the_activation_that_followed_it(
    tmp_path: Path,
) -> None:
    with chat_harness(tmp_path) as h:
        assert _narrowing_entry(h.repository, "sample", "v1") is None

        _audit(h, "catalog.activate", "v1")
        only_activation = _narrowing_entry(h.repository, "sample", "v1")
        assert only_activation is not None and only_activation["action"] == "catalog.activate"

        _audit(h, "setup.catalog.narrowed", "v1")
        _audit(h, "setup.catalog.narrowed", "v2")
        found = _narrowing_entry(h.repository, "sample", "v1")
        assert found is not None and found["action"] == "setup.catalog.narrowed"
        assert found["data"] == {"version": "v1"}
        assert _narrowing_entry(h.repository, "sample", "v3") is None


# --- closing proposals from the command line ---------------------------------------------


def test_a_proposal_that_changes_while_it_is_being_closed_is_left_to_whoever_changed_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose()

        def lose_the_race(*_: object, **__: object) -> None:
            raise ConflictError("the owner got there first")

        monkeypatch.setattr(SetupStore, "transition", lose_the_race)

        closed = run_cli(h.database, h.config_path, "proposal-decline", "--all")

        assert closed["declined"] == []
        monkeypatch.undo()
        assert h.status(proposal["proposalId"])["state"] == "waiting_for_owner"
        assert (
            h.rows("SELECT COUNT(*) FROM admin_audit WHERE action = 'setup.proposal.declined'")[0][
                0
            ]
            == 0
        )


# --- the setup connector's own catalog ---------------------------------------------------


def test_a_changed_setup_catalog_replaces_the_old_one_and_the_connector_keeps_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with chat_harness(tmp_path) as h:
        before = h.repository.get_active_catalog("adapter-setup").catalog_version
        assert h.ask("how_setup_works").status_code == 200

        monkeypatch.setattr(setup_catalog, "catalog_version", lambda: "setup-a-newer-one")
        returned = setup_catalog.ensure_setup_server(
            h.repository, ControlPlaneService(h.repository), "2026-10-05T10:05:00Z"
        )
        again = setup_catalog.ensure_setup_server(
            h.repository, ControlPlaneService(h.repository), "2026-10-05T10:06:00Z"
        )

        assert returned == again == "setup-a-newer-one"
        assert before != "setup-a-newer-one"
        assert h.repository.get_active_catalog("adapter-setup").catalog_version == returned
        assert h.ask("how_setup_works").status_code == 200  # the same token, the new catalog
        assert h.repository.verify_admin_audit_chain().ok


# --- an error inside errors --------------------------------------------------------------


def test_the_reason_an_owner_is_shown_is_the_one_at_the_bottom_of_nested_errors() -> None:
    inner = ValueError("the real reason")
    assert _leaf(inner) is inner

    nested: Any = inner
    for level in range(3):
        nested = ExceptionGroup(f"level {level}", [nested])
    assert _leaf(nested) is inner


def test_unwrapping_stops_after_a_sensible_depth() -> None:
    nested: Any = ValueError("buried too deep")
    for level in range(12):
        nested = ExceptionGroup(f"level {level}", [nested])

    found = _leaf(nested)

    assert isinstance(found, ExceptionGroup)  # eight steps down it gives up and reports that level
    assert str(found.args[0]) == "level 3"


def test_an_error_that_claims_members_but_has_none_is_reported_as_it_is() -> None:
    class Odd(Exception):
        exceptions = ()

    odd = Odd("no members")
    assert _leaf(odd) is odd


# --- pressing the same button again ------------------------------------------------------


def test_pressing_the_button_again_after_the_server_was_down_carries_on(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose()
        link = proposal["reviewLink"]
        path = _path(link)
        h.unlock(link)
        h.discovery.failure = RuntimeError("the server is down")
        first = h.post(f"{path}/start")
        assert "the server is down" in first.text
        assert h.status(proposal["proposalId"])["state"] == "connecting"
        draft = h.repository.list_connections("learn")

        h.discovery.failure = None
        second = h.post(f"{path}/start")

        assert second.status_code == 302
        assert h.status(proposal["proposalId"])["state"] == "in_review"
        assert h.repository.list_connections("learn") == draft  # the same draft, not a second one
        assert len(h.discovery.credentials) == 2
