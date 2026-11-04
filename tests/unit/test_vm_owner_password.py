# pyright: basic, reportArgumentType=false
"""`muse-vm chat-setup --set-owner-password`: asked for without echo, sent on stdin, never shown."""

from __future__ import annotations

import getpass
from typing import Any

import pytest

from muse_vm import cli
from tests.fixtures.vm_fakes import FakeTarget


def _answers(monkeypatch: pytest.MonkeyPatch, *typed: str) -> list[str]:
    asked: list[str] = []
    queue = list(typed)

    def fake(prompt: str = "") -> str:
        asked.append(prompt)
        return queue.pop(0)

    monkeypatch.setattr(getpass, "getpass", fake)
    return asked


def test_the_password_is_asked_for_twice_and_must_be_long_enough(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asked = _answers(
        monkeypatch,
        "short",  # too short: asked again without a confirmation
        "a long enough phrase",
        "a different long phrase",  # does not match
        "a long enough phrase",
        "a long enough phrase",
    )
    said: list[str] = []
    monkeypatch.setattr(cli, "say", lambda message="": said.append(message))
    assert cli._ask_owner_password() == "a long enough phrase"
    assert any("at least 10" in line for line in said)
    assert any("did not match" in line for line in said)
    assert len(asked) == 5 and all("assword" in p or "again" in p for p in asked)
    assert "a long enough phrase" not in "".join(said)  # never echoed
    assert capsys.readouterr().out == ""


class _Provider:
    def __init__(self, target: FakeTarget) -> None:
        self._target = target

    def target(self) -> FakeTarget:
        return self._target

    def restart_adapter(self, _: FakeTarget) -> None:
        return None


class _Admin:
    calls: list[tuple[tuple[str, ...], str | None]] = []
    answers: list[dict[str, Any]] = []

    def __init__(self, target: Any, actor: str = "muse-vm", dev_network: bool = False) -> None:
        del target, actor, dev_network

    def json(self, *args: str, timeout: float = 180, stdin: str | None = None) -> Any:
        del timeout
        _Admin.calls.append((args, stdin))
        return _Admin.answers.pop(0)


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> str:
    from muse_vm import bundle

    target = FakeTarget()
    target.put("config/serve.json", '{"publicUrl": "https://h", "chatSetup": true}')
    monkeypatch.setattr(cli, "_context", lambda args, ready=True: (object(), _Provider(target)))
    monkeypatch.setattr(cli, "Admin", _Admin)
    monkeypatch.setattr(bundle, "set_chat_setup", lambda t, enabled: False)
    said: list[str] = []
    monkeypatch.setattr(cli, "say", lambda message="": said.append(message))
    args = cli.build_parser().parse_args(["chat-setup", *argv])
    args.func(args)
    return "\n".join(said)


def test_the_password_goes_on_stdin_and_replaces_the_generated_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Admin.calls = []
    _Admin.answers = [
        {"ownerPasswordSet": True},
        {"connectorUrl": "u", "connectorNote": "n", "ownerCode": "ABCD-EFGH-JKMN"},
    ]
    _answers(monkeypatch, "my own long password", "my own long password")
    text = _run(monkeypatch, "--set-owner-password", "--rotate-owner-code")
    (first_args, first_stdin), (second_args, second_stdin) = _Admin.calls
    assert first_args == ("chat-setup-set-password",)
    assert first_stdin == "my own long password\n"  # on stdin, not in the arguments
    assert "my own long password" not in " ".join(first_args + second_args)
    assert second_args == ("chat-setup-enable",) and second_stdin is None  # no new code made
    assert "ABCD-EFGH-JKMN" not in text  # the generated code is not shown
    assert "owner password is set" in text and "my own long password" not in text


def test_without_the_flag_nothing_is_asked_and_the_code_is_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Admin.calls = []
    _Admin.answers = [{"connectorUrl": "u", "connectorNote": "n", "ownerCode": "WXYZ-2345-6789"}]
    asked = _answers(monkeypatch)
    text = _run(monkeypatch)
    assert asked == [] and _Admin.calls == [(("chat-setup-enable",), None)]
    assert "WXYZ-2345-6789" in text
