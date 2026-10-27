# pyright: basic
"""Telling the owner by a route the agent is not on."""

import socketserver
import threading
from collections.abc import Generator
from contextlib import contextmanager
from email import message_from_string
from email.message import Message
from typing import Any

import pytest

from muse_mcp_adapter.application.notify import HeldNotice, SmtpNotifier
from muse_mcp_adapter.application.serve import load_serve_config
from trustlab.hosting import serve
from trustlab.owner import OwnerSession
from trustlab.paths import AdapterPath, adapter_in_front_of
from trustlab.server import PersonalServer
from trustlab.world import load_world

from .test_owner_mode import CONTROLS, DANA, EXPOSED, PASSCODE, held


class Recorder:
    def __init__(self) -> None:
        self.notices: list[HeldNotice] = []

    def held(self, notice: HeldNotice) -> None:
        self.notices.append(notice)


class Exploding:
    def held(self, notice: HeldNotice) -> None:
        raise RuntimeError("the mail server is down")


@contextmanager
def lab(notifier: Any) -> Generator[tuple[PersonalServer, AdapterPath, OwnerSession]]:
    server = PersonalServer(load_world())
    with serve(server.asgi()) as port:
        url = f"http://127.0.0.1:{port}/mcp"
        with adapter_in_front_of(
            url, expose=EXPOSED, overrides=CONTROLS, owner_passcode=PASSCODE, notifier=notifier
        ) as adapter:
            owner = OwnerSession(PASSCODE)
            try:
                yield server, adapter, owner
            finally:
                owner.close()


def test_the_owner_is_told_once_when_an_action_is_held() -> None:
    recorder = Recorder()
    with lab(recorder) as (server, adapter, _):
        held(adapter.call("mail_send", DANA, key="k1"))
        held(adapter.call("mail_send", DANA, key="k1"))  # the agent asks again
        assert len(recorder.notices) == 1
        notice = recorder.notices[0]
        assert notice.operation == "mail_send"
        assert any("not approved before" in reason for reason in notice.reasons)
        assert server.state.outbox == []


def test_the_owners_link_is_not_the_one_the_agent_was_given_and_it_survives_a_reissue() -> None:
    recorder = Recorder()
    with lab(recorder) as (server, adapter, owner):
        agents_link = held(adapter.call("mail_send", DANA, key="k1"))
        held(adapter.call("mail_send", DANA, key="k1"))  # retires the first agent link
        owners_link = recorder.notices[0].link
        assert owners_link != agents_link
        assert owner.view(agents_link).status == 404
        view = owner.view(owners_link)
        assert view.status == 200 and view.arguments == DANA
        assert owner.decide(owners_link, approve=True).status_code == 200
        assert adapter.call("mail_send", DANA, key="k1").ok
        assert len(server.state.outbox) == 1


def test_the_owners_link_still_needs_the_passcode() -> None:
    recorder = Recorder()
    with lab(recorder) as (_, adapter, owner):
        held(adapter.call("mail_send", DANA, key="k1"))
        link = recorder.notices[0].link
        assert owner.decide(link, approve=True, passcode="not it").status_code == 403


def test_the_agent_never_sees_the_owners_link() -> None:
    recorder = Recorder()
    with lab(recorder) as (_, adapter, _):
        result = adapter.call("mail_send", DANA, key="k1")
        assert recorder.notices[0].link not in str(result.raw)
        again = adapter.call("mail_send", DANA, key="k1")
        assert recorder.notices[0].link not in str(again.raw)


def test_a_notice_that_cannot_be_sent_does_not_break_the_call() -> None:
    with lab(Exploding()) as (_, adapter, owner):
        link = held(adapter.call("mail_send", DANA, key="k1"))  # still held, link still given
        assert owner.decide(link, approve=True).status_code == 200
        assert adapter.call("mail_send", DANA, key="k1").ok


def test_without_a_notifier_everything_works_as_before() -> None:
    with lab(None) as (_, adapter, owner):
        owner.decide(held(adapter.call("mail_send", DANA, key="k1")), approve=True)
        assert adapter.call("mail_send", DANA, key="k1").ok


# --- the e-mail itself, against a small SMTP server --------------------------------------------


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        def say(text: str) -> None:
            self.wfile.write(text.encode() + b"\r\n")

        say("220 test")
        data: list[str] = []
        collecting = False
        for raw in self.rfile:
            line = raw.decode().rstrip("\r\n")
            if collecting:
                if line == ".":
                    collecting = False
                    self.server.messages.append("\n".join(data))  # type: ignore[attr-defined]
                    say("250 ok")
                else:
                    data.append(line)
                continue
            command = line.split(" ")[0].upper()
            if command == "DATA":
                collecting = True
                data = []
                say("354 go")
            elif command == "QUIT":
                say("221 bye")
                return
            else:
                say("250 ok")


@pytest.fixture
def smtp() -> Generator[tuple[int, list[str]]]:
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
    server.messages = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], server.messages  # type: ignore[attr-defined]
    server.shutdown()
    server.server_close()


NOTICE = HeldNotice(
    operation="mail_send",
    reasons=["It goes to someone you have not approved before: x@y.test"],
    link="https://adapter.example/approve/TOKEN",
    expires_at="2026-10-26T12:15:00Z",
)


def parse(raw: str) -> Message:
    return message_from_string(raw)


def test_the_email_says_what_is_waiting_and_carries_only_the_link(smtp) -> None:
    port, messages = smtp
    sent: list[tuple[bool, str]] = []
    SmtpNotifier(
        host="127.0.0.1",
        port=port,
        sender="adapter@home.test",
        to="owner@home.test",
        starttls=False,
        background=False,
        on_result=lambda ok, why: sent.append((ok, why)),
    ).held(NOTICE)
    assert sent == [(True, "sent")]
    mail = parse(messages[0])
    assert mail["To"] == "owner@home.test" and mail["From"] == "adapter@home.test"
    assert mail["Subject"] == "Waiting for your approval: mail_send"
    body = str(mail.get_payload(decode=True), "utf-8")  # type: ignore[arg-type]
    assert "https://adapter.example/approve/TOKEN" in body
    assert "not approved before: x@y.test" in body
    assert "passcode" in body and "ignore this message" in body


def test_an_unreachable_mail_server_is_reported_not_raised() -> None:
    sent: list[tuple[bool, str]] = []
    SmtpNotifier(
        host="127.0.0.1",
        port=1,
        sender="a@b.test",
        to="c@d.test",
        starttls=False,
        background=False,
        timeout=1,
        on_result=lambda ok, why: sent.append((ok, why)),
    ).held(NOTICE)
    assert sent and sent[0][0] is False


def test_sending_can_happen_in_the_background(smtp) -> None:
    port, messages = smtp
    done = threading.Event()
    SmtpNotifier(
        host="127.0.0.1",
        port=port,
        sender="a@b.test",
        to="c@d.test",
        starttls=False,
        on_result=lambda ok, why: done.set(),
    ).held(NOTICE)
    assert done.wait(5) and len(messages) == 1


def test_email_settings_are_read_from_the_service_configuration(tmp_path) -> None:
    import json

    path = tmp_path / "serve.json"
    path.write_text(
        json.dumps(
            {
                "publicUrl": "https://adapter.example",
                "approvalMode": "owner",
                "notifications": {
                    "email": {
                        "host": "smtp.example",
                        "port": 2525,
                        "from": "adapter@home.test",
                        "to": "owner@home.test",
                        "usernameEnv": "SMTP_USER",
                        "passwordEnv": "SMTP_PASS",
                    }
                },
            }
        )
    )
    email = load_serve_config(path).email
    assert email is not None
    assert (email.host, email.port, email.to, email.starttls) == (
        "smtp.example",
        2525,
        "owner@home.test",
        True,
    )
    assert (email.username_env, email.password_env) == ("SMTP_USER", "SMTP_PASS")


def test_no_notifications_section_means_no_email(tmp_path) -> None:
    import json

    path = tmp_path / "serve.json"
    path.write_text(json.dumps({"publicUrl": "https://adapter.example"}))
    assert load_serve_config(path).email is None
