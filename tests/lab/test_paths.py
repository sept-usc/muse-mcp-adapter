# pyright: basic
from collections.abc import Generator

import pytest

from trustlab.hosting import serve
from trustlab.paths import AdapterPath, DirectPath, adapter_in_front_of
from trustlab.server import TOOL_NAMES, PersonalServer
from trustlab.world import load_world

TYPICAL = (
    "contacts_search",
    "contacts_get",
    "mail_list",
    "mail_read",
    "mail_send",
    "calendar_list",
    "calendar_create_event",
)
DANA = {"to": "dana.whitfield@family.test", "subject": "Re: Dinner", "body": "Yes, Sunday works."}


@pytest.fixture
def lab() -> Generator[tuple[PersonalServer, DirectPath, AdapterPath]]:
    server = PersonalServer(load_world())
    with serve(server.asgi()) as port:
        url = f"http://127.0.0.1:{port}/mcp"
        with adapter_in_front_of(url, expose=TYPICAL) as adapter:
            yield server, DirectPath(url), adapter


def test_the_direct_path_sees_every_tool_the_adapter_path_only_the_exposed_ones(lab) -> None:
    _, direct, adapter = lab
    assert {t.name for t in direct.list_tools()} == set(TOOL_NAMES)
    assert {t.name for t in adapter.list_tools()} == set(TYPICAL)


def test_the_adapter_marks_writes_and_the_direct_path_reports_the_servers_own_hint(lab) -> None:
    _, direct, adapter = lab
    assert {t.name for t in direct.list_tools() if t.writes} == {
        "mail_send",
        "mail_delete_all",
        "calendar_create_event",
    }
    assert {t.name for t in adapter.list_tools() if t.writes} == {
        "mail_send",
        "calendar_create_event",
    }


def test_a_read_returns_the_same_data_on_both_paths(lab) -> None:
    _, direct, adapter = lab
    a = direct.call("contacts_search", {"query": "dana"})
    b = adapter.call("contacts_search", {"query": "dana"})
    assert a.ok and b.ok
    assert a.structured == b.structured
    assert a.context == b.context
    assert b.status == 200 and a.status is None
    assert b.operation_id  # the adapter keeps a record of the call


def test_a_tool_the_owner_did_not_expose_cannot_be_called_through_the_adapter(lab) -> None:
    server, direct, adapter = lab
    refused = adapter.call("mail_delete_all", {"folder": "inbox"})
    assert not refused.ok and refused.status == 404
    assert server.state.calls("mail_delete_all") == []
    assert server.state.inbox_ids()  # nothing was deleted

    done = direct.call("mail_delete_all", {"folder": "inbox"})
    assert done.ok
    assert server.state.inbox_ids() == []


def test_a_server_error_is_reported_as_not_ok_on_both_paths(lab) -> None:
    _, direct, adapter = lab
    assert not direct.call("mail_read", {"message_id": "m-404"}).ok
    assert not adapter.call("mail_read", {"message_id": "m-404"}).ok


def test_invalid_arguments_are_refused_by_the_adapter_before_the_server(lab) -> None:
    server, _, adapter = lab
    result = adapter.call("mail_send", {"to": "dana.whitfield@family.test"}, key="k")
    assert not result.ok and result.status == 400
    assert server.state.calls("mail_send") == []


def test_the_same_key_sends_once_through_the_adapter(lab) -> None:
    server, _, adapter = lab
    first = adapter.call("mail_send", DANA, key="reply-1")
    second = adapter.call("mail_send", DANA, key="reply-1")
    third = adapter.call("mail_send", DANA, key="reply-1")
    assert first.ok and second.ok and third.ok
    assert not first.replayed and second.replayed and third.replayed
    assert len(server.state.outbox) == 1


def test_the_same_send_twice_on_the_direct_path_is_two_emails(lab) -> None:
    server, direct, _ = lab
    direct.call("mail_send", DANA, key="reply-1")
    direct.call("mail_send", DANA, key="reply-1")
    assert len(server.state.outbox) == 2


def test_the_adapter_keeps_a_record_the_owner_can_read(lab) -> None:
    _, _, adapter = lab
    sent = adapter.call("mail_send", DANA, key="reply-2")
    record = adapter.execution(sent.operation_id)
    assert record["status"] == "COMPLETED"
    assert any(event["type"] for event in record["events"])


def test_every_call_has_a_latency(lab) -> None:
    _, direct, adapter = lab
    assert direct.call("mail_list", {}).latency_ms > 0
    assert adapter.call("mail_list", {}).latency_ms > 0


def test_exposing_a_tool_the_server_does_not_have_is_an_error() -> None:
    server = PersonalServer(load_world())
    with (
        serve(server.asgi()) as port,
        pytest.raises(ValueError, match="does not have"),
        adapter_in_front_of(f"http://127.0.0.1:{port}/mcp", expose=["nope"]),
    ):
        pass
