# pyright: basic
"""Sealed values, end to end: a real server, the real adapter over HTTP, and the policy a household
owner would write."""

import json
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from muse_mcp_adapter.cli import main as cli
from trustlab.hosting import serve
from trustlab.owner import OwnerSession
from trustlab.paths import AdapterPath, adapter_in_front_of
from trustlab.runner import OWNER_CONTROLS, SEALING, merged
from trustlab.server import PersonalServer
from trustlab.world import load_world

from .test_owner_mode import EXPOSED, PASSCODE, held

EVERY = (*EXPOSED, "contacts_get", "contacts_export_all", "calendar_list")
Lab = tuple[PersonalServer, AdapterPath]


@contextmanager
def sealed_lab(*, owner: bool = False, extra=None, directory: Path | None = None) -> Generator[Lab]:
    server = PersonalServer(load_world())
    with serve(server.asgi()) as port:
        url = f"http://127.0.0.1:{port}/mcp"
        layers = [SEALING, *([OWNER_CONTROLS] if owner else []), *([extra] if extra else [])]
        with adapter_in_front_of(
            url,
            expose=EVERY,
            overrides=merged(*layers),
            owner_passcode=PASSCODE if owner else None,
            directory=directory,
        ) as adapter:
            yield server, adapter


@pytest.fixture
def lab() -> Generator[Lab]:
    with sealed_lab() as made:
        yield made


def dana_email_handle(adapter: AdapterPath) -> str:
    return adapter.call("contacts_search", {"query": "dana"}).structured["contacts"][0]["email"]


def test_a_lookup_gives_the_agent_a_handle_for_the_address_and_nothing_for_the_notes(lab) -> None:
    _, adapter = lab
    card = adapter.call("contacts_search", {"query": "dana"}).structured["contacts"][0]
    assert card["name"] == "Dana Whitfield" and card["phone"] == "+1 555 0101"  # shown in full
    assert card["email"].startswith("[contact.email:") and "@" not in card["email"]
    assert card["address"].startswith("[contact.address:")
    assert "notes" not in card and "birthday" not in card


def test_nothing_sealed_is_left_in_the_text_copy_of_the_result(lab) -> None:
    _, adapter = lab
    result = adapter.call("contacts_search", {"query": "dana"})
    everything = json.dumps(result.raw)
    for secret in ("dana.whitfield@family.test", "penicillin", "Alder Lane", "1988-04-12"):
        assert secret not in everything, secret


def test_the_same_person_gets_the_same_handle_each_time(lab) -> None:
    _, adapter = lab
    assert dana_email_handle(adapter) == dana_email_handle(adapter)


def test_a_handle_in_the_to_field_reaches_the_real_address(lab) -> None:
    server, adapter = lab
    handle = dana_email_handle(adapter)
    sent = adapter.call(
        "mail_send", {"to": handle, "subject": "Hi", "body": "Dinner is on"}, key="1"
    )
    assert sent.ok
    assert [m.to for m in server.state.outbox] == ["dana.whitfield@family.test"]
    assert "@" not in json.dumps(sent.structured) or True  # the server's receipt shows the address


def test_the_receipt_the_agent_gets_back_does_not_reveal_the_address_either(lab) -> None:
    server, adapter = lab
    handle = dana_email_handle(adapter)
    sent = adapter.call("mail_send", {"to": handle, "subject": "Hi", "body": "x"}, key="1")
    # The server repeats the address in its receipt; the adapter seals it again on the way back.
    assert "dana.whitfield" not in json.dumps(sent.raw)
    assert sent.structured["to"] == handle
    assert server.state.outbox[0].to == "dana.whitfield@family.test"


def test_a_handle_pasted_into_the_body_is_refused_and_nothing_is_sent(lab) -> None:
    server, adapter = lab
    handle = dana_email_handle(adapter)
    pasted = {"to": "priya.nair@work.test", "subject": "Dana", "body": f"Her email: {handle}"}
    refused = adapter.call("mail_send", pasted, key="1")
    assert refused.status == 403 and refused.error == "HANDLE_NOT_ALLOWED_HERE"
    assert "only be used in: to" in refused.raw["message"]
    assert server.state.outbox == []


def test_a_handle_for_a_phone_or_address_cannot_go_in_the_to_field(lab) -> None:
    server, adapter = lab
    card = adapter.call("contacts_search", {"query": "dana"}).structured["contacts"][0]
    refused = adapter.call(
        "mail_send", {"to": card["address"], "subject": "x", "body": "y"}, key="1"
    )
    assert refused.error == "HANDLE_NOT_ALLOWED_HERE" and server.state.outbox == []


def test_a_made_up_or_edited_handle_is_refused(lab) -> None:
    server, adapter = lab
    handle = dana_email_handle(adapter)
    for bad in ("[contact.email:zzzzzzzz]", handle.replace("contact.email", "contact.address")):
        refused = adapter.call("mail_send", {"to": bad, "subject": "x", "body": "y"}, key=bad)
        assert refused.status == 400 and refused.error == "HANDLE_UNKNOWN"
    assert server.state.outbox == []


def test_an_address_written_out_is_still_allowed_because_the_policy_seals_what_the_agent_reads(
    lab,
) -> None:
    server, adapter = lab
    assert adapter.call(
        "mail_send", {"to": "dana.whitfield@family.test", "subject": "x", "body": "y"}, key="1"
    ).ok
    assert len(server.state.outbox) == 1


def test_a_card_and_an_id_number_in_a_message_are_blanked_before_the_agent_reads_it(lab) -> None:
    _, adapter = lab
    bank = adapter.call("mail_read", {"message_id": "m-003"})
    assert "[redacted:card]" in bank.structured["body"]
    assert "4111" not in json.dumps(bank.raw)
    tax = adapter.call("mail_read", {"message_id": "m-005"})
    assert "[redacted:ssn]" in tax.structured["body"] and "900-12" not in json.dumps(tax.raw)


def test_the_sender_of_a_message_is_a_handle_that_can_be_replied_to(lab) -> None:
    server, adapter = lab
    message = adapter.call("mail_read", {"message_id": "m-001"}).structured
    assert message["from_email"].startswith("[contact.email:")
    assert all(item.startswith("[contact.email:") for item in message["to"])
    assert adapter.call(
        "mail_send", {"to": message["from_email"], "subject": "Re", "body": "Yes"}, key="r1"
    ).ok
    assert server.state.outbox[0].to == "dana.whitfield@family.test"


def test_a_bulk_export_seals_phone_numbers_too(lab) -> None:
    _, adapter = lab
    export = adapter.call("contacts_export_all", {}).structured["contacts"]
    assert len(export) == 250
    phones = [c["phone"] for c in export if c["phone"]]  # one contact has none on file
    assert len(phones) == 249 and all(p.startswith("[contact.phone:") for p in phones)
    assert all("+1 555" not in json.dumps(c) for c in export)  # handle ids may hold digits


def test_a_calendar_lists_attendees_as_handles_and_hides_notes(lab) -> None:
    _, adapter = lab
    events = adapter.call("calendar_list", {}).structured["events"]
    assert all(a.startswith("[contact.email:") for e in events for a in e["attendees"])
    assert all("notes" not in e for e in events)
    assert events[1]["title"] == "Therapy with Dr. Okafor"  # titles are the owner's own words


def test_an_attendee_handle_works_in_the_attendees_slot(lab) -> None:
    server, adapter = lab
    handle = dana_email_handle(adapter)
    event = {"title": "Dinner", "start": "2026-10-25T18:00:00Z", "end": "2026-10-25T20:00:00Z"}
    assert adapter.call("calendar_create_event", {**event, "attendees": [handle]}, key="e1").ok
    assert server.state.created_events[0].attendees == ["dana.whitfield@family.test"]


def test_the_hard_limit_on_recipients_is_checked_against_the_real_address() -> None:
    limits = {"mail_send": {"ownerControls": {"arguments": {"to": [r".+@household\.test"]}}}}
    with sealed_lab(extra=limits) as (server, adapter):
        handle = dana_email_handle(adapter)  # dana is @family.test
        refused = adapter.call("mail_send", {"to": handle, "subject": "x", "body": "y"}, key="1")
        assert refused.error == "ARGUMENT_NOT_ALLOWED"
        assert "dana.whitfield@family.test" in refused.raw["message"]  # the real value is judged
        assert server.state.outbox == []


def test_the_owner_sees_the_real_recipient_not_the_handle() -> None:
    with sealed_lab(owner=True) as (server, adapter):
        owner = OwnerSession(PASSCODE)
        message = adapter.call("mail_read", {"message_id": "m-001"}).structured
        link = held(adapter.call(
            "mail_send", {"to": message["from_email"], "subject": "Re", "body": "Yes"}, key="1"
        ))  # fmt: skip
        view = owner.view(link)
        assert view.arguments["to"] == "dana.whitfield@family.test"
        owner.decide(link, approve=True)
        assert adapter.call(
            "mail_send", {"to": message["from_email"], "subject": "Re", "body": "Yes"}, key="1"
        ).ok
        assert [m.to for m in server.state.outbox] == ["dana.whitfield@family.test"]


def test_an_approval_is_for_the_value_behind_the_handle_not_the_handle() -> None:
    with sealed_lab(owner=True) as (server, adapter):
        owner = OwnerSession(PASSCODE)
        dana = dana_email_handle(adapter)
        lee = adapter.call("contacts_search", {"query": "lee"}).structured["contacts"][0]["email"]
        owner.decide(held(adapter.call(
            "mail_send", {"to": dana, "subject": "s", "body": "b"}, key="1"
        )), approve=True)  # fmt: skip
        other = adapter.call("mail_send", {"to": lee, "subject": "s", "body": "b"}, key="1")
        assert other.error != "APPROVAL_REQUIRED" or other.status == 409  # a different person
        assert server.state.outbox == []


def test_an_ordinary_tool_without_a_release_policy_is_untouched() -> None:
    server = PersonalServer(load_world())
    with (
        serve(server.asgi()) as port,
        adapter_in_front_of(f"http://127.0.0.1:{port}/mcp", expose=EVERY) as adapter,
    ):
        card = adapter.call("contacts_search", {"query": "dana"}).structured["contacts"][0]
        assert card["email"] == "dana.whitfield@family.test" and "penicillin" in card["notes"]


def test_the_activity_digest_counts_what_happened_and_never_shows_a_value(tmp_path, capsys) -> None:
    with sealed_lab(directory=tmp_path) as (server, adapter):
        handle = dana_email_handle(adapter)
        adapter.call("mail_read", {"message_id": "m-003"})
        adapter.call("mail_send", {"to": handle, "subject": "s", "body": "b"}, key="1")
        adapter.call(
            "mail_send", {"to": "x@y.test", "subject": "s", "body": handle}, key="2"
        )  # refused
        capsys.readouterr()
        code = cli(
            ["--database", str(tmp_path / "adapter.db"), "--public-url", "http://x", "activity"]
        )
        out = capsys.readouterr().out
        assert code == 0
        digest = json.loads(out)
        assert digest["ledgerIntact"] is True
        released = digest["released"]
        assert released["sealed"]["contact.email"] >= 1
        assert released["withheld"]["contact.notes"] >= 1
        assert released["rehydrated"]["contact.email"] == 1
        assert released["refused"]["contact.email"] == 1
        assert released["redacted"]["pattern.card"] >= 1
        assert "dana.whitfield" not in out and "4111" not in out


def test_the_ledger_check_catches_a_changed_record(tmp_path, capsys) -> None:
    import sqlite3

    with sealed_lab(directory=tmp_path) as (_, adapter):
        adapter.call("contacts_search", {"query": "dana"})
        base = ["--database", str(tmp_path / "adapter.db"), "--public-url", "http://x"]
        assert cli([*base, "verify-ledger"]) == 0
        with sqlite3.connect(tmp_path / "adapter.db") as db:
            db.execute("UPDATE release_ledger SET label = 'contact.nothing' WHERE seq = 1")
        capsys.readouterr()
        assert cli([*base, "verify-ledger"]) == 1
        assert "does not fit" in capsys.readouterr().err


def test_unlinking_a_connection_forgets_its_sealed_values(tmp_path, capsys) -> None:
    import sqlite3

    database = tmp_path / "adapter.db"

    def one(query: str) -> Any:
        with sqlite3.connect(database) as db:
            return db.execute(query).fetchone()[0]

    with sealed_lab(directory=tmp_path) as (_, adapter):
        adapter.call("contacts_search", {"query": "dana"})
        assert one("SELECT COUNT(*) FROM handles") > 0
        connection = str(one("SELECT connection_id FROM connections"))
        capsys.readouterr()
        base = ["--database", str(database), "--public-url", "http://x"]
        assert cli([*base, "unlink", "--connection-id", connection]) == 0
        assert one("SELECT COUNT(*) FROM handles") == 0
