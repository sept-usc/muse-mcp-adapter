"""Bringing the Tailscale device up: sign-in, approval, and what the tailnet must allow."""

import json
from typing import Any

import pytest

from muse_vm import ingress
from muse_vm.runner import Result
from tests.fixtures.vm_fakes import FakeTarget

NAME = "muse-adapter-x.tail1.ts.net"
HAVE: dict[str, Any] = {"https": [], "funnel": []}
ONLY_HTTPS: dict[str, Any] = {"https": []}


def _no_sleep(_seconds: float) -> None:
    return None


def _running(caps: dict[str, Any]) -> str:
    return json.dumps({"BackendState": "Running", "Self": {"DNSName": NAME + ".", "CapMap": caps}})


class Tailnet(FakeTarget):
    """Answers the commands the bring-up runs, with a scripted sequence of statuses."""

    def __init__(self, states: list[str], *, published: bool = True, feature_url: str = "") -> None:
        super().__init__()
        self.states = states
        self.published = published
        self.feature_url = feature_url
        self.cert_attempts = 0
        self.cert_ok = True

    def sh(self, script: str, *args: Any, **kwargs: Any) -> Result:  # type: ignore[override]
        super().sh(script, *args, **kwargs)
        if "tailscale status --json" in script:
            current = self.states[0] if len(self.states) == 1 else self.states.pop(0)
            return Result(0, current, "")
        if "serve status --json" in script:
            published = {"AllowFunnel": {NAME + ":443": True}} if self.published else {}
            return Result(0, json.dumps(published), "")
        if "query-feature" in script:
            return Result(
                0, json.dumps({"URL": self.feature_url}) if self.feature_url else "{}", ""
            )
        if "tailscale cert" in script:
            self.cert_attempts += 1
            return Result(0 if self.cert_ok else 1, "", "" if self.cert_ok else "set-dns: deadline")
        if "logs" in script:
            return Result(0, "To authenticate, visit: https://login.tailscale.com/a/abc123XYZ", "")
        return Result(0, "", "")


def _up(target: Tailnet, **kwargs: Any) -> tuple[str, list[str]]:
    said: list[str] = []
    name = ingress.hostname_after_start(target, say=said.append, sleep=lambda _: None, **kwargs)
    return name, said


def test_a_sign_in_link_is_shown_and_the_name_returned_once_funnel_is_published() -> None:
    target = Tailnet([json.dumps({"BackendState": "NeedsLogin"}), _running(HAVE)])
    name, said = _up(target)
    assert name == NAME
    assert any("login.tailscale.com/a/abc123XYZ" in line for line in said)
    assert target.cert_attempts == 1  # the certificate is fetched before anyone visits


def test_a_tailnet_without_funnel_is_told_exactly_what_to_allow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ingress, "EXTENSION", 0.05)
    target = Tailnet([_running(ONLY_HTTPS)])  # HTTPS is on, Funnel is not
    with pytest.raises(SystemExit, match="HTTPS and Funnel were not allowed"):
        _up(target, timeout=0.01)
    said: list[str] = []
    with pytest.raises(SystemExit):
        ingress.hostname_after_start(target, timeout=0.01, say=said.append, sleep=lambda _: None)
    message = "\n".join(said)
    assert '"attr": ["funnel"]' in message and "/admin/acls" in message
    assert "/admin/dns" not in message  # HTTPS is already on
    assert message.count("does not allow HTTPS and Funnel") == 1  # said once, not every poll


def test_the_one_click_link_from_tailscale_is_preferred_when_it_gives_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ingress, "EXTENSION", 0.05)
    target = Tailnet([_running({})], feature_url="https://login.tailscale.com/f/funnel?q=abc")
    said: list[str] = []
    with pytest.raises(SystemExit):
        ingress.hostname_after_start(target, timeout=0.01, say=said.append, sleep=lambda _: None)
    assert any("https://login.tailscale.com/f/funnel?q=abc" in line for line in said)


def test_the_wait_ends_successfully_when_the_owner_allows_funnel_meanwhile() -> None:
    target = Tailnet([_running(ONLY_HTTPS), _running(HAVE)])
    name, said = _up(target)
    assert name == NAME and any("does not allow" in line for line in said)


def test_device_approval_is_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    target = Tailnet([json.dumps({"BackendState": "NeedsMachineAuth"}), _running(HAVE)])
    name, said = _up(target)
    assert name == NAME and any(
        "device approval" in line and "/admin/machines" in line for line in said
    )


def test_a_device_that_never_signs_in_ends_with_a_clear_message() -> None:
    target = Tailnet([json.dumps({"BackendState": "Stopped"})])
    with pytest.raises(SystemExit, match="did not appear"):
        _up(target, timeout=0.05)


def test_a_failing_certificate_fetch_is_retried_and_reported_but_does_not_stop_the_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ingress.time, "sleep", _no_sleep)
    target = Tailnet([_running(HAVE)])
    target.cert_ok = False
    name, said = _up(target)
    assert name == NAME and target.cert_attempts == 3
    assert any("no HTTPS certificate" in line and "set-dns" in line for line in said)
