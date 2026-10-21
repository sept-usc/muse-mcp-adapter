"""A brand-new public name can take a while to reach public DNS. That is a note, not a failure."""

import socket
import urllib.error
from typing import Any

import pytest

from muse_vm import selfcheck
from muse_vm.selfcheck import NAME_PENDING, Report
from tests.fixtures.vm_fakes import FakeTarget

URL = "https://muse-adapter-x.tail1.ts.net"


def _unresolved(*_a: Any, **_k: Any) -> Any:
    raise urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided, or not known"))


def _refused(*_a: Any, **_k: Any) -> Any:
    raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))


def _quiet(_message: str) -> None:
    return None


def _sleepless(_seconds: float) -> None:
    return None


def _waits_for(result: tuple[bool, str]) -> Any:
    def wait(*_a: Any, **_k: Any) -> tuple[bool, str]:
        return result

    return wait


def _inside_is(ok: bool) -> Any:
    def inside(_target: Any) -> Report:
        return _inside(ok)

    return inside


def _answers(status: int, text: str) -> Any:
    def request(*_a: Any, **_k: Any) -> tuple[int, dict[str, str], str]:
        return status, {}, text

    return request


def _inside(ok: bool) -> Report:
    report = Report()
    report.add("egress guard rules installed", ok, "" if ok else "missing")
    return report


def _wait(monkeypatch: pytest.MonkeyPatch, raiser: Any) -> tuple[bool, str]:
    monkeypatch.setattr(selfcheck, "_request", raiser)
    return selfcheck.wait_healthy(URL, timeout=0.05, say=_quiet, sleep=_sleepless)


def test_a_name_that_does_not_resolve_is_reported_as_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    healthy, why = _wait(monkeypatch, _unresolved)
    assert not healthy and why.startswith(NAME_PENDING)


def test_a_refused_connection_is_a_real_failure_not_a_pending_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    healthy, why = _wait(monkeypatch, _refused)
    assert not healthy and not why.startswith(NAME_PENDING) and "refused" in why


def test_a_name_that_resolves_but_answers_with_an_error_is_a_real_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(selfcheck, "_request", _answers(502, "bad gateway"))
    healthy, why = selfcheck.wait_healthy(URL, timeout=0.05, say=_quiet, sleep=_sleepless)
    assert not healthy and why == "HTTP 502"


def test_pending_dns_with_a_healthy_inside_does_not_fail_the_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(selfcheck, "wait_healthy", _waits_for((False, f"{NAME_PENDING}: x")))
    monkeypatch.setattr(selfcheck, "inside_checks", _inside_is(True))
    report = selfcheck.run(FakeTarget(), URL, wait=0, say=_quiet)
    assert report.ok and report.address_pending
    shown = "\n".join(report.lines())
    assert "! public HTTPS /health answers" in shown and "up to 20 minutes" in shown


def test_pending_dns_with_a_broken_inside_still_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selfcheck, "wait_healthy", _waits_for((False, f"{NAME_PENDING}: x")))
    monkeypatch.setattr(selfcheck, "inside_checks", _inside_is(False))
    report = selfcheck.run(FakeTarget(), URL, wait=0, say=_quiet)
    assert not report.ok and not report.address_pending


def test_a_real_public_failure_is_not_softened(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selfcheck, "wait_healthy", _waits_for((False, "HTTP 502")))
    monkeypatch.setattr(selfcheck, "inside_checks", _inside_is(True))
    report = selfcheck.run(FakeTarget(), URL, wait=0, say=_quiet)
    assert not report.ok and not report.address_pending


class _Clock:
    """A clock that only moves when the code sleeps, so waiting minutes takes no time."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_a_name_with_no_dns_record_stops_the_wait_after_the_grace_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(selfcheck, "_request", _unresolved)
    clock = _Clock()
    said: list[str] = []
    healthy, why = selfcheck.wait_healthy(
        URL, timeout=600, say=said.append, sleep=clock.sleep, clock=clock, dns_grace=240
    )
    assert not healthy and why.startswith(NAME_PENDING)
    assert 240 <= clock.now < 260  # not the full ten minutes of silence
    assert any("published by Tailscale" in line for line in said)


def test_the_wait_reports_progress_every_half_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selfcheck, "_request", _unresolved)
    clock = _Clock()
    said: list[str] = []
    selfcheck.wait_healthy(
        URL, timeout=600, say=said.append, sleep=clock.sleep, clock=clock, dns_grace=100
    )
    notes = [line for line in said if "still waiting after" in line]
    assert notes[0].startswith("  still waiting after 0m30s") and len(notes) >= 2
    assert "no public DNS record yet" in notes[0]
    assert "Ctrl-C" in said[0]  # it tells the person they can stop waiting


def test_a_real_failure_keeps_the_full_wait_and_names_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(selfcheck, "_request", _refused)
    clock = _Clock()
    said: list[str] = []
    healthy, why = selfcheck.wait_healthy(
        URL, timeout=600, say=said.append, sleep=clock.sleep, clock=clock, dns_grace=240
    )
    assert not healthy and "refused" in why and clock.now >= 600  # grace applies to DNS only


def test_ctrl_c_while_waiting_stops_the_wait_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def interrupted(*_a: Any, **_k: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return _unresolved()

    monkeypatch.setattr(selfcheck, "_request", interrupted)
    clock = _Clock()
    said: list[str] = []
    healthy, why = selfcheck.wait_healthy(
        URL, timeout=600, say=said.append, sleep=clock.sleep, clock=clock
    )
    assert not healthy and why.startswith(NAME_PENDING)  # the install can then be kept
    assert any("stopped waiting" in line for line in said)
