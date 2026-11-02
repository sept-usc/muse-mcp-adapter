# pyright: basic, reportArgumentType=false
"""`muse-vm chat-setup`: turning on setup by chatting with Muse for a deployment."""

import json
from typing import Any

import pytest

from muse_vm import bundle, cli
from tests.fixtures.vm_fakes import FakeTarget


def _target_with_config(**extra: Any) -> FakeTarget:
    target = FakeTarget()
    settings = json.loads(bundle.serve_config("adapter.example.ts.net"))
    settings.update(extra)
    target.put("config/serve.json", json.dumps(settings, indent=2) + "\n")
    return target


def test_the_flag_is_written_only_when_asked_and_read_back() -> None:
    assert "chatSetup" not in bundle.serve_config("h.example")
    assert json.loads(bundle.serve_config("h.example", chat_setup=True))["chatSetup"] is True
    assert bundle.chat_setup_enabled(bundle.serve_config("h.example", chat_setup=True))
    assert not bundle.chat_setup_enabled(bundle.serve_config("h.example"))
    assert not bundle.chat_setup_enabled(None) and not bundle.chat_setup_enabled("not json")
    assert not bundle.chat_setup_enabled("[1, 2]")


def test_turning_it_on_changes_one_setting_and_reports_whether_it_changed() -> None:
    target = _target_with_config()
    before = json.loads(target.text("config/serve.json"))
    assert bundle.set_chat_setup(target, True) is True
    after = json.loads(target.text("config/serve.json"))
    assert after.pop("chatSetup") is True and after == before  # nothing else moved
    assert bundle.set_chat_setup(target, True) is False
    assert bundle.set_chat_setup(target, False) is True
    assert "chatSetup" not in json.loads(target.text("config/serve.json"))
    assert bundle.set_chat_setup(target, False) is False


def test_there_is_nothing_to_turn_on_before_the_adapter_is_installed() -> None:
    with pytest.raises(SystemExit, match="finish `muse-vm setup`"):
        bundle.set_chat_setup(FakeTarget(), True)


def test_a_new_hostname_keeps_chat_setup_on() -> None:
    target = _target_with_config(chatSetup=True)
    assert bundle.set_hostname(target, "new-name.example.ts.net") is True
    settings = json.loads(target.text("config/serve.json"))
    assert settings["publicUrl"] == "https://new-name.example.ts.net"
    assert settings["chatSetup"] is True
    assert bundle.set_hostname(target, "new-name.example.ts.net") is False


class _Provider:
    def __init__(self, target: FakeTarget) -> None:
        self._target = target
        self.restarted = 0

    def target(self) -> FakeTarget:
        return self._target

    def restart_adapter(self, _: FakeTarget) -> None:
        self.restarted += 1


class _Admin:
    answers: list[dict[str, Any]] = []
    calls: list[tuple[str, ...]] = []

    def __init__(self, target: Any, actor: str = "muse-vm", dev_network: bool = False) -> None:
        del target, actor, dev_network

    def json(self, *args: str, timeout: float = 180) -> Any:
        del timeout
        _Admin.calls.append(args)
        return _Admin.answers.pop(0)


def _run(monkeypatch: pytest.MonkeyPatch, target: FakeTarget, *argv: str) -> tuple[_Provider, str]:
    provider = _Provider(target)
    monkeypatch.setattr(cli, "_context", lambda args, ready=True: (object(), provider))
    monkeypatch.setattr(cli, "Admin", _Admin)
    said: list[str] = []
    monkeypatch.setattr(cli, "say", lambda message="": said.append(message))
    args = cli.build_parser().parse_args(["chat-setup", *argv])
    args.func(args)
    return provider, "\n".join(said)


def test_it_turns_the_feature_on_restarts_once_and_shows_secrets_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Admin.calls = []
    _Admin.answers = [
        {
            "token": "mcpa_the_token",
            "museInstructions": 'Tell Muse: "Create a connector called Adapter setup from the API '
            'spec at https://h/c/x/openapi.json." '
            "If Muse asks for a bearer token instead, give it: mcpa_the_token",
            "ownerCode": "ABCD-EFGH-JKMN",
        }
    ]
    provider, text = _run(monkeypatch, _target_with_config())
    assert provider.restarted == 1
    assert bundle.chat_setup_enabled(provider.target().text("config/serve.json"))
    assert _Admin.calls == [("chat-setup-enable",)]
    assert "Create a connector called Adapter setup" in text
    assert text.count("mcpa_the_token") == 1  # shown once, not repeated in the sentence
    assert "ABCD-EFGH-JKMN" in text and "never into Muse" in text


def test_running_it_again_does_not_restart_or_repeat_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Admin.calls = []
    _Admin.answers = [
        {
            "connectorUrl": "https://h/c/x/openapi.json",
            "connectorNote": "The setup connector already exists.",
            "ownerCodeNote": "An owner code is already set.",
        }
    ]
    provider, text = _run(monkeypatch, _target_with_config(chatSetup=True))
    assert provider.restarted == 0
    assert "already exists" in text and "already set" in text and "mcpa_" not in text


def test_a_new_owner_code_can_be_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    _Admin.calls = []
    _Admin.answers = [{"ownerCode": "WXYZ-2345-6789", "connectorUrl": "u", "connectorNote": "n"}]
    _run(monkeypatch, _target_with_config(chatSetup=True), "--rotate-owner-code")
    assert _Admin.calls == [("chat-setup-enable", "--rotate-owner-code")]
