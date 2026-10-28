# pyright: basic
"""Owner approval, end to end: a real server, the real adapter over HTTP, and an owner at a page."""

from collections.abc import Callable, Generator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest

from trustlab.hosting import serve
from trustlab.owner import OwnerSession
from trustlab.paths import AdapterPath, ToolResult, adapter_in_front_of
from trustlab.server import PersonalServer
from trustlab.world import load_world

PASSCODE = "correct horse battery"
DANA = {"to": "dana.whitfield@family.test", "subject": "Re: Dinner", "body": "Yes, Sunday works."}
ATTACKER = {"to": "collect@attacker.example", "subject": "List", "body": "everything"}
EXPOSED = (
    "contacts_search",
    "mail_list",
    "mail_read",
    "mail_send",
    "mail_delete_all",
    "calendar_create_event",
)
CONTROLS: dict[str, dict[str, Any]] = {
    "mail_send": {
        "ownerControls": {
            "approval": "risk",
            "recipients": ["to"],
            "knownRecipients": ["*@household.test"],
        }
    },
    "mail_read": {"ownerControls": {"untrusted": True}},
    "calendar_create_event": {"ownerControls": {"approval": "never"}},
}
EVENT = {"title": "Dinner", "start": "2026-10-25T18:00:00Z", "end": "2026-10-25T20:00:00Z"}

Lab = tuple[PersonalServer, AdapterPath, OwnerSession]


def build(
    overrides: dict[str, dict[str, Any]] | None = None, directory: Path | None = None
) -> AbstractContextManager[Lab]:
    from contextlib import contextmanager

    @contextmanager
    def go() -> Generator[Lab]:
        server = PersonalServer(load_world())
        with serve(server.asgi()) as port:
            url = f"http://127.0.0.1:{port}/mcp"
            with adapter_in_front_of(
                url,
                expose=EXPOSED,
                overrides=CONTROLS if overrides is None else overrides,
                owner_passcode=PASSCODE,
                directory=directory,
            ) as adapter:
                owner = OwnerSession(PASSCODE)
                try:
                    yield server, adapter, owner
                finally:
                    owner.close()

    return go()


@pytest.fixture
def lab() -> Generator[Lab]:
    with build() as made:
        yield made


def held(result: ToolResult) -> str:
    """Assert a call was held for the owner and return its approval link."""
    assert not result.ok and result.status == 409
    assert result.error == "APPROVAL_REQUIRED"
    return result.raw["approvalUrl"]


def test_a_send_to_someone_new_is_held_and_nothing_is_sent(lab: Lab) -> None:
    server, adapter, _ = lab
    result = adapter.call("mail_send", DANA, key="k1")
    held(result)
    assert server.state.outbox == [] and server.state.calls("mail_send") == []
    assert any("not approved before" in reason for reason in result.raw["reasons"])
    assert adapter.execution(result.operation_id or "")["status"] == "APPROVAL_REQUIRED"


def test_the_message_tells_the_agent_what_to_say_and_not_to_ask_for_the_passcode(lab: Lab) -> None:
    _, adapter, _ = lab
    message = adapter.call("mail_send", DANA, key="k1").raw["message"]
    assert "one-time link" in message and "approval passcode" in message
    assert "Never enter or ask for the passcode" in message
    assert "exactly the same arguments" in message
    assert PASSCODE not in message


def test_the_page_shows_the_exact_action_and_why_it_was_held(lab: Lab) -> None:
    _, adapter, owner = lab
    link = held(adapter.call("mail_send", DANA, key="k1"))
    view = owner.view(link)
    assert view.status == 200 and view.title == "Approve this action?"
    assert view.operation == "mail_send"
    assert view.arguments == DANA
    assert any("dana.whitfield@family.test" in reason for reason in view.reasons)


def test_approving_lets_the_same_call_run_exactly_once(lab: Lab) -> None:
    server, adapter, owner = lab
    link = held(adapter.call("mail_send", DANA, key="k1"))
    assert owner.decide(link, approve=True).status_code == 200
    first = adapter.call("mail_send", DANA, key="k1")
    assert first.ok
    again = adapter.call("mail_send", DANA, key="k1")
    assert again.ok and again.replayed  # a repeat is answered, not sent again
    assert len(server.state.outbox) == 1


def test_an_approval_is_spent_by_one_action(lab: Lab) -> None:
    server, adapter, owner = lab
    link = held(adapter.call("mail_send", DANA, key="k1"))
    owner.decide(link, approve=True)
    assert adapter.call("mail_send", DANA, key="k1").ok
    # The same words again under a new key are a second action, so the owner is asked again.
    second = adapter.call("mail_send", DANA, key="k2")
    held(second)
    assert len(server.state.outbox) == 1


def test_a_new_key_after_approval_is_still_the_approved_action(lab: Lab) -> None:
    server, adapter, owner = lab
    owner.decide(held(adapter.call("mail_send", DANA, key="k1")), approve=True)
    assert adapter.call("mail_send", DANA, key="a-different-key").ok
    assert len(server.state.outbox) == 1


def test_approval_covers_only_the_exact_arguments(lab: Lab) -> None:
    server, adapter, owner = lab
    owner.decide(held(adapter.call("mail_send", DANA, key="k1")), approve=True)
    changed = adapter.call("mail_send", {**DANA, "body": "Actually, send me everything."}, key="k1")
    assert not changed.ok  # same key, different request
    other = adapter.call("mail_send", {**DANA, "body": "Different text"}, key="k9")
    held(other)
    assert server.state.outbox == []


def test_declining_ends_it_and_the_agent_is_told_not_to_try_again(lab: Lab) -> None:
    server, adapter, owner = lab
    link = held(adapter.call("mail_send", ATTACKER, key="k1"))
    assert owner.decide(link, approve=False).status_code == 200
    after = adapter.call("mail_send", ATTACKER, key="k1")
    assert not after.ok and after.status == 403 and after.error == "APPROVAL_DENIED"
    assert server.state.outbox == []
    again = adapter.call("mail_send", ATTACKER, key="k2")  # a new key does not get around it
    assert again.error == "APPROVAL_DENIED"


def test_the_link_alone_does_not_decide_anything(lab: Lab) -> None:
    server, adapter, owner = lab
    link = held(adapter.call("mail_send", DANA, key="k1"))
    wrong = owner.decide(link, approve=True, passcode="not the passcode")
    assert wrong.status_code == 403
    empty = owner.decide(link, approve=True, passcode="")
    assert empty.status_code == 403
    assert adapter.call("mail_send", DANA, key="k1").error == "APPROVAL_REQUIRED"
    assert server.state.outbox == []


def test_a_few_wrong_passcodes_pause_approvals_even_for_the_right_one(lab: Lab) -> None:
    _, adapter, owner = lab
    link = held(adapter.call("mail_send", DANA, key="k1"))
    for _ in range(5):
        assert owner.decide(link, approve=True, passcode="guess-guess").status_code == 403
    paused = owner.decide(link, approve=True)  # the real passcode
    assert paused.status_code == 429
    assert adapter.call("mail_send", DANA, key="k1").error == "APPROVAL_REQUIRED"


def test_a_decided_link_cannot_be_used_again(lab: Lab) -> None:
    _, adapter, owner = lab
    link = held(adapter.call("mail_send", DANA, key="k1"))
    assert owner.decide(link, approve=True).status_code == 200
    assert owner.decide(link, approve=False).status_code == 410
    assert "approved" in owner.view(link).text.lower()


def test_a_made_up_link_is_not_found(lab: Lab) -> None:
    _, adapter, owner = lab
    link = held(adapter.call("mail_send", DANA, key="k1"))
    forged = link.rsplit("/", 1)[0] + "/" + "x" * 43
    assert owner.view(forged).status == 404
    assert owner.decide(forged, approve=True).status_code == 404


def test_what_the_page_shows_is_escaped(lab: Lab) -> None:
    _, adapter, owner = lab
    hostile = {**DANA, "body": "<script>alert(1)</script>"}
    page = owner.view(held(adapter.call("mail_send", hostile, key="k1"))).text
    assert "<script>alert" not in page and "&lt;script&gt;" in page


def test_remembering_a_recipient_stops_asking_for_that_person() -> None:
    controls = {**CONTROLS, "mail_read": {}}  # nothing counts as untrusted in this test
    with build(controls) as (server, adapter, owner):
        owner.decide(held(adapter.call("mail_send", DANA, key="k1")), approve=True, remember=True)
        adapter.call("mail_send", DANA, key="k1")
        later = adapter.call("mail_send", {**DANA, "body": "See you there"}, key="k2")
        assert later.ok  # Dana is now someone the owner has accepted
        assert len(server.state.outbox) == 2
        stranger = adapter.call("mail_send", ATTACKER, key="k3")
        held(stranger)


def test_an_address_the_owner_listed_is_not_asked_about() -> None:
    with build() as (server, adapter, _):
        lee = {"to": "lee.park@household.test", "subject": "Hi", "body": "Home at 6"}
        assert adapter.call("mail_send", lee, key="k1").ok
        assert len(server.state.outbox) == 1


def test_a_send_right_after_reading_someone_elses_mail_is_held_even_to_a_known_address(
    lab: Lab,
) -> None:
    server, adapter, owner = lab
    lee = {"to": "lee.park@household.test", "subject": "Hi", "body": "Home at 6"}
    assert adapter.call("mail_send", lee, key="before").ok  # nothing read yet: allowed
    adapter.call("mail_read", {"message_id": "m-004"})
    after = adapter.call("mail_send", {**lee, "body": "Home at 7"}, key="after")
    held(after)
    assert any("written by someone else" in reason for reason in after.raw["reasons"])
    assert len(server.state.outbox) == 1


def test_a_destructive_tool_is_always_held_and_cannot_be_switched_to_never() -> None:
    with build({"mail_delete_all": {"ownerControls": {"approval": "always"}}}) as made:
        server, adapter, owner = made
        link = held(adapter.call("mail_delete_all", {"folder": "inbox"}, key="d1"))
        assert any("cannot be undone" in r for r in owner.view(link).reasons)
        assert server.state.calls("mail_delete_all") == []
        owner.decide(link, approve=True)
        assert adapter.call("mail_delete_all", {"folder": "inbox"}, key="d1").ok
        assert server.state.inbox_ids() == []
    with (
        pytest.raises(Exception, match="Catalog validation failed"),
        build({"mail_delete_all": {"ownerControls": {"approval": "never"}}}),
    ):
        pass


def test_a_tool_the_owner_set_to_never_runs_without_asking(lab: Lab) -> None:
    server, adapter, _ = lab
    assert adapter.call("calendar_create_event", EVENT, key="e1").ok
    assert len(server.state.created_events) == 1


def test_reads_are_never_held(lab: Lab) -> None:
    _, adapter, _ = lab
    assert adapter.call("mail_list", {}).ok
    assert adapter.call("mail_read", {"message_id": "m-001"}).ok
    assert adapter.call("contacts_search", {"query": "dana"}).ok


def test_without_controls_every_write_waits_for_the_owner() -> None:
    with build({}) as (server, adapter, owner):
        result = adapter.call("calendar_create_event", EVENT, key="e1")
        link = held(result)
        assert any("approve every use" in r for r in result.raw["reasons"])
        owner.decide(link, approve=True)
        assert adapter.call("calendar_create_event", EVENT, key="e1").ok
        assert len(server.state.created_events) == 1


def test_a_hard_limit_on_an_argument_refuses_without_ever_asking_the_owner() -> None:
    limits = {
        "mail_send": {
            "ownerControls": {"approval": "always", "arguments": {"to": [r".+@household\.test"]}}
        }
    }
    with build(limits) as (server, adapter, owner):
        refused = adapter.call("mail_send", ATTACKER, key="k1")
        assert refused.status == 403 and refused.error == "ARGUMENT_NOT_ALLOWED"
        assert "collect@attacker.example" in refused.raw["message"]
        allowed = adapter.call("mail_send", {**DANA, "to": "lee.park@household.test"}, key="k2")
        held(allowed)  # inside the limit, so it goes on to the owner
        assert server.state.outbox == []


def test_hard_limits_apply_in_every_approval_mode() -> None:
    server = PersonalServer(load_world())
    limits = {"mail_send": {"ownerControls": {"arguments": {"to": [r".+@household\.test"]}}}}
    with (
        serve(server.asgi()) as port,
        adapter_in_front_of(
            f"http://127.0.0.1:{port}/mcp", expose=EXPOSED, overrides=limits
        ) as adapter,
    ):
        assert adapter.call("mail_send", ATTACKER, key="k1").error == "ARGUMENT_NOT_ALLOWED"
        lee = {"to": "lee.park@household.test", "subject": "Hi", "body": "x"}
        assert adapter.call("mail_send", lee, key="k2").ok  # Muse's own mode: the call is the OK
        assert [m.to for m in server.state.outbox] == ["lee.park@household.test"]


def test_hard_limits_also_reach_into_lists() -> None:
    limits = {
        "calendar_create_event": {
            "ownerControls": {"approval": "never", "arguments": {"attendees": [r".+@family\.test"]}}
        }
    }
    with build(limits) as (server, adapter, _):
        bad = adapter.call(
            "calendar_create_event", {**EVENT, "attendees": ["x@evil.test"]}, key="1"
        )
        assert bad.error == "ARGUMENT_NOT_ALLOWED"
        ok = adapter.call(
            "calendar_create_event", {**EVENT, "attendees": ["dana.whitfield@family.test"]}, key="2"
        )
        assert ok.ok and len(server.state.created_events) == 1


def test_too_many_waiting_actions_are_refused() -> None:
    with build({}) as (_, adapter, _owner):
        for number in range(20):
            held(
                adapter.call(
                    "calendar_create_event", {**EVENT, "title": f"t{number}"}, key=f"k{number}"
                )
            )
        over = adapter.call("calendar_create_event", {**EVENT, "title": "one more"}, key="over")
        assert over.status == 403 and over.error == "ARGUMENT_NOT_ALLOWED"
        assert "waiting for the owner" in over.raw["message"]


def test_asking_again_while_waiting_gives_a_fresh_link_and_retires_the_old_one(lab: Lab) -> None:
    server, adapter, owner = lab
    first = held(adapter.call("mail_send", DANA, key="k1"))
    second = held(adapter.call("mail_send", DANA, key="k1"))
    assert first != second
    assert owner.view(first).status == 404  # the lost or stale link no longer opens anything
    assert owner.decide(second, approve=True).status_code == 200
    assert adapter.call("mail_send", DANA, key="k1").ok
    assert len(server.state.outbox) == 1  # one request all along, one email


def test_invalid_owner_controls_are_rejected_when_publishing() -> None:
    bad: list[Callable[[], dict[str, dict[str, Any]]]] = [
        lambda: {"mail_send": {"ownerControls": {"approval": "sometimes"}}},
        lambda: {"mail_send": {"ownerControls": {"surprise": True}}},
        lambda: {"mail_send": {"ownerControls": {"arguments": {"to": ["(unclosed"]}}}},
        lambda: {"mail_send": {"ownerControls": "yes"}},
        lambda: {"mail_send": {"ownerControls": {"untrustedWindowSeconds": 0}}},
    ]
    for make in bad:
        with pytest.raises(Exception, match="Catalog validation failed"), build(make()):
            pass


def test_owner_mode_refuses_to_start_without_a_passcode(tmp_path: Path) -> None:
    from muse_mcp_adapter.application.serve import ServeConfig, build_application
    from muse_mcp_adapter.domain.errors import InvalidStateError
    from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository

    database = tmp_path / "a.db"
    with SqliteControlPlaneRepository(database) as repository:
        repository.initialize()
        config = ServeConfig(
            public_url="http://127.0.0.1:1", allow_insecure_http=True, approval_mode="owner"
        )
        with pytest.raises(InvalidStateError, match="approval passcode"):
            build_application(repository, config, {}, database=database)


class Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        from datetime import UTC, datetime

        self.now = datetime.now(UTC)

    def __call__(self) -> str:
        return self.now.isoformat().replace("+00:00", "Z")

    def advance(self, seconds: int) -> None:
        from datetime import timedelta

        self.now += timedelta(seconds=seconds)


def test_an_unanswered_request_expires_and_the_link_stops_working() -> None:
    clock = Clock()
    server = PersonalServer(load_world())
    with (
        serve(server.asgi()) as port,
        adapter_in_front_of(
            f"http://127.0.0.1:{port}/mcp",
            expose=EXPOSED,
            overrides=CONTROLS,
            owner_passcode=PASSCODE,
            clock=clock,
        ) as adapter,
    ):
        owner = OwnerSession(PASSCODE)
        link = held(adapter.call("mail_send", DANA, key="k1"))
        clock.advance(901)
        assert owner.view(link).status == 410
        assert owner.decide(link, approve=True).status_code == 410
        fresh = held(adapter.call("mail_send", DANA, key="k1"))  # asking again starts over
        assert fresh != link
        assert server.state.outbox == []


def test_an_approval_that_is_not_used_in_time_lapses() -> None:
    clock = Clock()
    server = PersonalServer(load_world())
    with (
        serve(server.asgi()) as port,
        adapter_in_front_of(
            f"http://127.0.0.1:{port}/mcp",
            expose=EXPOSED,
            overrides=CONTROLS,
            owner_passcode=PASSCODE,
            clock=clock,
        ) as adapter,
    ):
        owner = OwnerSession(PASSCODE)
        owner.decide(held(adapter.call("mail_send", DANA, key="k1")), approve=True)
        clock.advance(901)  # the agent comes back too late
        assert adapter.call("mail_send", DANA, key="k1").error == "APPROVAL_REQUIRED"
        assert server.state.outbox == []


def test_the_owner_can_decide_from_the_command_line(tmp_path: Path, capsys) -> None:
    import json

    from muse_mcp_adapter.cli import main as cli

    database = str(tmp_path / "adapter.db")

    def run(*args: str) -> Any:
        capsys.readouterr()
        code = cli(["--database", database, "--public-url", "http://127.0.0.1:1", *args])
        out = capsys.readouterr()
        return code, (json.loads(out.out) if out.out.strip() else None), out.err

    with build(directory=tmp_path) as (server, adapter, _):
        # build() put the adapter's database in tmp_path/adapter.db
        held(adapter.call("mail_send", DANA, key="k1"))
        code, listed, _ = run("approvals")
        assert code == 0 and len(listed) == 1
        assert listed[0]["operation"] == "mail_send" and listed[0]["arguments"] == DANA
        code, decided, _ = run(
            "approval-decide", "--request-id", listed[0]["requestId"], "--approve", "--remember"
        )
        assert code == 0 and decided["status"] == "approved"
        assert run("approvals")[1] == []  # nothing is waiting any more
        assert adapter.call("mail_send", DANA, key="k1").ok
        assert len(server.state.outbox) == 1
        code, _, err = run("approval-decide", "--request-id", listed[0]["requestId"], "--decline")
        assert code == 1 and "no longer waiting" in err


def test_the_passcode_is_set_from_the_environment_and_a_short_one_is_refused(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    from muse_mcp_adapter.cli import main as cli

    database = str(tmp_path / "a.db")
    base = ["--database", database, "--public-url", "http://127.0.0.1:1"]
    monkeypatch.setenv("TEST_PASSCODE", "short")
    assert cli([*base, "owner-passcode", "--passcode-env", "TEST_PASSCODE"]) == 1
    assert "at least" in capsys.readouterr().err
    monkeypatch.setenv("TEST_PASSCODE", "a long enough one")
    assert cli([*base, "owner-passcode", "--passcode-env", "TEST_PASSCODE"]) == 0
    assert "a long enough one" not in capsys.readouterr().out
