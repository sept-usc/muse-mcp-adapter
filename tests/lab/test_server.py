# pyright: basic
from collections.abc import Generator

import pytest

from trustlab.hosting import serve
from trustlab.mcp_direct import McpDirect
from trustlab.server import TOOL_NAMES, PersonalServer
from trustlab.world import load_world


@pytest.fixture
def stand_in() -> Generator[tuple[PersonalServer, McpDirect]]:
    server = PersonalServer(load_world())
    with serve(server.asgi()) as port:
        yield server, McpDirect(f"http://127.0.0.1:{port}/mcp")


def test_it_offers_exactly_the_nine_tools_with_honest_hints(stand_in) -> None:
    _, client = stand_in
    tools = {tool["name"]: tool for tool in client.list_tools()}
    assert set(tools) == set(TOOL_NAMES)
    assert tools["contacts_search"]["annotations"]["readOnlyHint"] is True
    assert tools["mail_send"]["annotations"]["readOnlyHint"] is False
    assert tools["mail_delete_all"]["annotations"]["destructiveHint"] is True
    assert all(tool["outputSchema"] for tool in tools.values())


def test_search_finds_a_contact_with_the_full_card(stand_in) -> None:
    _, client = stand_in
    result = client.call("contacts_search", {"query": "dana"})
    assert not result["isError"]
    [card] = result["structured"]["contacts"]
    assert card["phone"] == "+1 555 0101"
    assert "penicillin" in card["notes"]  # the server hands over the whole card


def test_search_is_capped_but_reports_the_true_total(stand_in) -> None:
    _, client = stand_in
    result = client.call("contacts_search", {"query": "directory"})
    assert result["structured"]["total"] == 244
    assert len(result["structured"]["contacts"]) == 25


def test_export_returns_every_contact(stand_in) -> None:
    _, client = stand_in
    result = client.call("contacts_export_all", {})
    assert len(result["structured"]["contacts"]) == 250


def test_mail_list_is_newest_first_and_read_returns_the_body(stand_in) -> None:
    _, client = stand_in
    listing = client.call("mail_list", {})["structured"]
    assert listing["total"] == 6
    assert listing["messages"][0]["id"] == "m-006"
    body = client.call("mail_read", {"message_id": "m-004"})["structured"]["body"]
    assert "collect@attacker.example" in body


def test_reading_a_missing_message_is_an_error_not_a_crash(stand_in) -> None:
    _, client = stand_in
    assert client.call("mail_read", {"message_id": "m-404"})["isError"] is True


def test_sending_mail_lands_in_the_outbox_and_the_ledger(stand_in) -> None:
    server, client = stand_in
    receipt = client.call(
        "mail_send", {"to": "dana.whitfield@family.test", "subject": "Re", "body": "Yes"}
    )["structured"]
    assert receipt == {"sent": True, "message_id": "sent-001", "to": "dana.whitfield@family.test"}
    assert [m.to for m in server.state.outbox] == ["dana.whitfield@family.test"]
    [effect] = server.state.calls("mail_send")
    assert effect.kind == "write" and effect.arguments["body"] == "Yes"


def test_a_repeated_send_is_sent_twice_because_the_server_has_no_memory(stand_in) -> None:
    server, client = stand_in
    args = {"to": "dana.whitfield@family.test", "subject": "Re", "body": "Yes"}
    client.call("mail_send", args)
    client.call("mail_send", args)
    assert len(server.state.outbox) == 2


def test_delete_all_empties_the_folder(stand_in) -> None:
    server, client = stand_in
    assert client.call("mail_delete_all", {"folder": "inbox"})["structured"]["deleted"] == 6
    assert server.state.inbox_ids() == []
    assert client.call("mail_list", {})["structured"]["total"] == 0
    assert client.call("mail_read", {"message_id": "m-001"})["isError"] is True
    assert server.state.calls("mail_delete_all")[0].kind == "destructive"


def test_calendar_lists_and_creates(stand_in) -> None:
    server, client = stand_in
    assert len(client.call("calendar_list", {})["structured"]["events"]) == 3
    created = client.call(
        "calendar_create_event",
        {"title": "Dinner", "start": "2026-10-25T18:00:00Z", "end": "2026-10-25T20:00:00Z"},
    )["structured"]
    assert created["id"] == "e-new-001"
    assert len(client.call("calendar_list", {})["structured"]["events"]) == 4
    assert len(server.state.created_events) == 1


def test_the_ledger_records_reads_in_order(stand_in) -> None:
    server, client = stand_in
    client.call("contacts_search", {"query": "sam"})
    client.call("mail_list", {})
    assert [(e.sequence, e.tool, e.kind) for e in server.state.effects] == [
        (1, "contacts_search", "read"),
        (2, "mail_list", "read"),
    ]
