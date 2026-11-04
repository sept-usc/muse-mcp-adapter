"""The steps behind the pages that only go wrong when something else has gone wrong first.

A sign-in service that is not there, a link that fails half way, a publish repeated after a
crash, a change made stale underneath. Each test makes one of those happen against the real,
fully wired service, and checks what is left behind: nothing exposed, nothing half made, no secret.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from muse_mcp_adapter.catalog.canonical import freeze_json
from muse_mcp_adapter.chat_setup.compile import Computed, finalize
from muse_mcp_adapter.chat_setup.service import (
    _clean_client_id,  # pyright: ignore[reportPrivateUsage]
    _clean_optional_secret,  # pyright: ignore[reportPrivateUsage]
    _clean_secret,  # pyright: ignore[reportPrivateUsage]
)
from muse_mcp_adapter.domain.errors import (
    ConflictError,
    ControlPlaneError,
    EntityNotFoundError,
    RequestValidationError,
)
from muse_mcp_adapter.domain.models import DataPlaneRequest
from tests.fixtures.chat_setup import ChatHarness, chat_harness, connector_from, run_cli


def _path(link_or_path: str) -> str:
    return re.sub(r"^https?://[^/]+", "", link_or_path)


def _text(html: str) -> str:
    return " ".join(
        re.sub(r"<[^>]+>", " ", re.sub(r"<style.*?</style>", "", html, flags=re.S)).split()
    )


def _token_of(link: str) -> str:
    return link.rsplit("/", 1)[1]


def _connect(h: ChatHarness, **fields: object) -> dict[str, Any]:
    proposal = h.propose(**fields)
    assert h.approve(proposal["reviewLink"], h.review(proposal["reviewLink"])).status_code == 302
    return proposal


def _request(h: ChatHarness, operation: str) -> DataPlaneRequest:
    return DataPlaneRequest(
        request_id="r-1",
        server_id="adapter-setup",
        public_operation_id=operation,
        local_identity_id="owner",
        connection_id=h.repository.list_connections("adapter-setup")[0].connection_id,
        arguments=cast(Mapping[str, object], freeze_json({})),
        idempotency_key=None,
    )


def _rows(h: ChatHarness, action: str) -> int:
    return int(h.rows(f"SELECT COUNT(*) FROM admin_audit WHERE action = '{action}'")[0][0])


# --- what the agent can reach ------------------------------------------------------------


def test_a_setup_call_for_something_the_service_does_not_have_is_refused(tmp_path: Path) -> None:
    with (
        chat_harness(tmp_path) as h,
        pytest.raises(EntityNotFoundError, match="approve_everything is not available"),
    ):
        h.service.execute(_request(h, "approve_everything"))


# --- signing in --------------------------------------------------------------------------


def test_signing_in_is_refused_plainly_when_the_deployment_cannot_do_it(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        h.service._login_links = None  # pyright: ignore[reportPrivateUsage]
        proposal = h.propose(server="linear")
        h.unlock(proposal["reviewLink"])

        started = h.post(f"{_path(proposal['reviewLink'])}/start", plan="oauth")

        assert "not available on this deployment" in _text(started.text)
        assert h.status(proposal["proposalId"])["state"] == "connecting"  # the owner can try again
        with pytest.raises(EntityNotFoundError):
            h.repository.get_active_catalog("linear")  # nothing was put in place


# --- choosing how to sign in -------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "said"),
    [
        ({"plan": "bogus"}, "Choose how to sign in"),
        ({"plan": "env_token", "secret": ""}, "Enter the token for this service"),
        ({"plan": "env_token", "secret": "   "}, "Enter the token for this service"),
        ({"plan": "env_token", "secret": "x" * 4097}, "does not look like a token"),
        ({"plan": "env_token", "secret": "tok\x01en"}, "does not look like a token"),
        ({"plan": "oauth_app", "client_id": "has space", "client_secret": "s"}, "client ID"),
        ({"plan": "oauth_app", "client_id": "", "client_secret": "s"}, "client ID"),
        ({"plan": "oauth_app", "client_id": "x" * 501, "client_secret": "s"}, "client ID"),
        (
            {"plan": "oauth_app", "client_id": "id", "client_secret": "s" * 4097},
            "does not look like a client secret",
        ),
        (
            {"plan": "oauth_app", "client_id": "id", "client_secret": "se\x02cret"},
            "does not look like a client secret",
        ),
    ],
)
def test_what_the_owner_types_is_checked_before_anything_is_made(
    tmp_path: Path, fields: dict[str, str], said: str
) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.ask(
            "propose_connection",
            {"endpoint": "https://other.example/mcp", "userWords": "an unlisted server"},
        ).json()["structuredContent"]
        h.unlock(proposal["reviewLink"])

        started = h.post(f"{_path(proposal['reviewLink'])}/start", **fields)

        assert said in _text(started.text)
        assert h.status(proposal["proposalId"])["state"] == "waiting_for_owner"
        assert not h.repository.list_connections("other-example")
        assert (
            h.rows("SELECT COUNT(*) FROM owner_secrets WHERE name LIKE 'CHAT_SETUP_%'")[0][0] == 0
        )


def test_the_cleaners_keep_what_is_fine_and_refuse_what_is_not() -> None:
    assert _clean_client_id("  abc123  ") == "abc123"
    assert _clean_optional_secret("") is None
    assert _clean_optional_secret(None) is None
    assert _clean_optional_secret("   ") is None
    assert _clean_optional_secret(" s3cret ") == "s3cret"
    assert _clean_secret("  tok  ") == "tok"
    for bad in (None, "", "  ", "x" * 4097, "a\x00b"):
        with pytest.raises(RequestValidationError):
            _clean_secret(bad)
    for bad in (None, "", "two words", "x" * 501, "tab\tid"):
        with pytest.raises(RequestValidationError):
            _clean_client_id(bad)


# --- a step that fails half way ----------------------------------------------------------


def test_a_failure_while_linking_leaves_no_token_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.ask(
            "propose_connection",
            {"endpoint": "https://other.example/mcp", "userWords": "needs a token"},
        ).json()["structuredContent"]
        h.unlock(proposal["reviewLink"])

        def fail(**_: object) -> object:
            raise ControlPlaneError("the connection could not be made")

        monkeypatch.setattr(h.service._connections, "link", fail)  # pyright: ignore[reportPrivateUsage]

        started = h.post(
            f"{_path(proposal['reviewLink'])}/start", plan="env_token", secret="ghp_secret_value"
        )

        assert "could not be made" in _text(started.text)
        assert (
            h.rows("SELECT COUNT(*) FROM owner_secrets WHERE name LIKE 'CHAT_SETUP_%'")[0][0] == 0
        )
        assert h.status(proposal["proposalId"])["state"] == "waiting_for_owner"
        assert "ghp_secret_value" not in started.text


def test_a_failure_while_saving_the_owners_app_undoes_the_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose(server="asana")
        h.unlock(proposal["reviewLink"])

        def fail(*_: object, **__: object) -> None:
            raise ControlPlaneError("the app could not be saved")

        monkeypatch.setattr(h.service._store, "put_oauth_client", fail)  # pyright: ignore[reportPrivateUsage]

        started = h.post(
            f"{_path(proposal['reviewLink'])}/start",
            client_id="the-client",
            client_secret="the-secret",
        )

        assert "could not be saved" in _text(started.text)
        connections = h.repository.list_connections("asana")
        assert connections and all(item.revoked_at is not None for item in connections)
        assert h.repository.get_server("asana").enabled is False
        assert h.status(proposal["proposalId"])["state"] == "waiting_for_owner"


def test_nothing_is_left_to_undo_when_a_draft_was_never_made(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        before = h.rows("SELECT COUNT(*) FROM connections")[0][0]

        h.service._undo_draft(None, None)  # pyright: ignore[reportPrivateUsage]

        assert h.rows("SELECT COUNT(*) FROM connections")[0][0] == before


def test_undoing_a_draft_keeps_the_server_while_another_connection_uses_it(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        _connect(h)
        kept = h.repository.list_connections("learn")[0].connection_id
        run_cli(
            h.database, h.config_path, "link", "--server-id", "learn", "--label", "draft",
            "--credential", "none",
        )  # fmt: skip
        draft = next(c for c in h.repository.list_connections("learn") if c.connection_id != kept)

        h.service._undo_draft("learn", draft.connection_id)  # pyright: ignore[reportPrivateUsage]
        assert h.repository.get_server("learn").enabled is True
        h.service._undo_draft("learn", draft.connection_id)  # pyright: ignore[reportPrivateUsage]

        assert [c.revoked_at is None for c in h.repository.list_connections("learn")].count(
            True
        ) == 1


# --- the token the owner copies ----------------------------------------------------------


def test_the_token_is_revealed_only_once_the_change_is_in_place(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose()
        link = proposal["reviewLink"]
        waiting = h.service.lookup(_token_of(link))
        assert waiting is not None and h.service.reveal(waiting) is None

        page = h.review(link)
        reviewing = h.service.lookup(_token_of(link))
        assert reviewing is not None and h.service.reveal(reviewing) is None

        assert h.approve(link, page).status_code == 302
        applied = h.service.lookup(_token_of(link))
        assert applied is not None
        shown = h.service.reveal(applied)
        assert shown is not None and shown.token.startswith("mcpa_")
        assert shown.api_description_url.endswith("/openapi.json")
        assert shown.token == connector_from(h.get(link).text)[1]

        h.service.collected(applied)
        assert h.service.reveal(h.service.lookup(_token_of(link))) is None  # type: ignore[arg-type]


def test_a_fresh_link_ends_the_old_one_and_only_an_open_proposal_gets_one(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        proposal = h.propose()
        old = proposal["reviewLink"]

        fresh = h.service.reissue_link(proposal["proposalId"])

        assert fresh != old and fresh.startswith(old.rsplit("/", 1)[0])
        assert h.get(old).status_code == 404
        assert h.get(fresh).status_code == 200

        h.unlock(fresh)
        assert h.post(f"{_path(fresh)}/cancel").status_code == 302  # now it is closed
        with pytest.raises(ConflictError, match="open proposal"):
            h.service.reissue_link(proposal["proposalId"])


# --- publishing and activating again after a crash ---------------------------------------


def test_publishing_and_activating_the_same_version_again_changes_nothing(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        proposal = _connect(h, readOnly=True)
        stored = h.service._store.get_proposal(proposal["proposalId"])  # pyright: ignore[reportPrivateUsage]
        assert stored.state == "applied" and stored.applied is not None
        version = str(stored.applied["catalogVersion"])
        computed = Computed.from_json(stored.computed or {})
        final = finalize(computed, computed.selected, [])
        snapshot = h.repository.get_discovery(int(stored.snapshot_id or 0))
        activations = _rows(h, "catalog.activate")
        published = _rows(h, "catalog.publish")

        again = h.service._publish("learn", snapshot, version, final.policies)  # pyright: ignore[reportPrivateUsage]
        h.service._activate("learn", version)  # pyright: ignore[reportPrivateUsage]

        assert again.catalog_digest == stored.applied["catalogDigest"]
        assert h.repository.get_active_catalog("learn").catalog_version == version
        assert _rows(h, "catalog.activate") == activations  # it was already live
        assert _rows(h, "catalog.publish") == published + 1  # the attempt is recorded
        assert h.repository.verify_admin_audit_chain().ok


def test_failing_a_proposal_that_already_moved_on_changes_nothing(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        proposal = _connect(h)
        stored = h.service._store.get_proposal(proposal["proposalId"])  # pyright: ignore[reportPrivateUsage]
        failures = _rows(h, "setup.proposal.failed")

        h.service._fail(stored, "too late")  # pyright: ignore[reportPrivateUsage]

        assert h.status(proposal["proposalId"])["state"] == "ready"
        assert _rows(h, "setup.proposal.failed") == failures


# --- narrowing, held to its own rule -----------------------------------------------------


def test_a_narrowing_that_would_give_more_is_refused_even_if_asked_for(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        _connect(h, readOnly=True)
        live = h.repository.get_active_catalog("learn")
        item = live.primitives[0]
        wider = dict(item.policy)
        wider["timeoutSeconds"] = int(str(item.policy["timeoutSeconds"])) + 100

        with pytest.raises(ConflictError, match="give Muse more"):
            h.service._narrow(  # pyright: ignore[reportPrivateUsage]
                live, lambda _: True, edited={item.primitive_key: wider}, prefix="test"
            )

        assert h.repository.get_active_catalog("learn").catalog_version == live.catalog_version


def test_a_narrowing_made_from_a_catalog_that_is_no_longer_live_is_refused(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        _connect(h, readOnly=True)
        stale = h.repository.get_active_catalog("learn")
        connection = h.repository.list_connections("learn")[0].connection_id
        assert (
            h.ask(
                "turn_off_operations", {"connectionId": connection, "operations": ["get_issue"]}
            ).status_code
            == 200
        )

        with pytest.raises(ConflictError, match="changed while the change was being made"):
            h.service._narrow(stale, lambda _: True, prefix="test")  # pyright: ignore[reportPrivateUsage]


def test_a_server_with_nothing_published_exposes_nothing(tmp_path: Path) -> None:
    with chat_harness(tmp_path) as h:
        assert h.service.operations("never-heard-of-it") == []
        assert h.service.operations("learn") == []  # registered by nobody yet
