"""The owner's side of an approval, played over HTTP exactly as a person with a browser would.

It opens the one-time link, reads what the page shows, and posts a decision with the passcode.
Nothing here reaches into the adapter's database: if the page does not show the exact action, the
owner cannot see it, and a scenario that depends on it fails.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Any, cast

import httpx

from muse_mcp_adapter.application.notify import HeldNotice


@dataclass(frozen=True)
class PageView:
    status: int
    title: str
    operation: str
    reasons: tuple[str, ...]
    arguments: dict[str, Any]
    text: str


class OwnerSession:
    def __init__(self, passcode: str) -> None:
        self._passcode = passcode
        self._http = httpx.Client(timeout=30)

    def view(self, url: str) -> PageView:
        response = self._http.get(url)
        body = response.text
        title = _find(r"<h1>(.*?)</h1>", body)
        operation = _find(r"wants to run <b>(.*?)</b>", body)
        reasons = tuple(
            html.unescape(item) for item in re.findall(r"<li>(.*?)</li>", body, re.DOTALL)
        )
        shown = _find(r"<pre[^>]*>(.*?)</pre>", body)
        arguments: dict[str, Any] = {}
        try:
            parsed: Any = json.loads(shown) if shown else {}
            if isinstance(parsed, dict):
                arguments = {str(k): v for k, v in cast("dict[Any, Any]", parsed).items()}
        except json.JSONDecodeError:
            pass
        return PageView(response.status_code, title, operation, reasons, arguments, body)

    def decide(
        self, url: str, *, approve: bool, passcode: str | None = None, remember: bool = False
    ) -> httpx.Response:
        form = {
            "passcode": self._passcode if passcode is None else passcode,
            "decision": "approve" if approve else "decline",
        }
        if remember:
            form["remember"] = "yes"
        return self._http.post(url, data=form)

    def close(self) -> None:
        self._http.close()


def _find(pattern: str, body: str) -> str:
    match = re.search(pattern, body, re.DOTALL)
    return html.unescape(match.group(1)) if match else ""


@dataclass(frozen=True)
class Verdict:
    approve: bool
    remember: bool = False


class Owner:
    """Someone who gets a link and answers it. Subclasses differ only in how they judge."""

    kind = "owner"

    def __init__(self, passcode: str) -> None:
        self.session = OwnerSession(passcode)

    def answer(self, link: str, expected: tuple[str, ...]) -> tuple[PageView, Verdict]:
        view = self.session.view(link)
        verdict = self.judge(view, expected)
        self.session.decide(link, approve=verdict.approve, remember=verdict.remember)
        return view, verdict

    def judge(self, view: PageView, expected: tuple[str, ...]) -> Verdict:
        raise NotImplementedError

    def close(self) -> None:
        self.session.close()


class VigilantOwner(Owner):
    """Reads the page and approves only what they asked for: every recipient is one they named."""

    kind = "vigilant"

    def judge(self, view: PageView, expected: tuple[str, ...]) -> Verdict:
        recipients = _recipients(view.arguments)
        ok = bool(recipients) and all(item.lower() in expected for item in recipients)
        return Verdict(approve=ok, remember=ok)


class CarelessOwner(Owner):
    """Approves whatever is shown without reading it. The limit of any approval scheme."""

    kind = "careless"

    def judge(self, view: PageView, expected: tuple[str, ...]) -> Verdict:
        return Verdict(approve=True)


def _recipients(arguments: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for key in ("to", "cc", "bcc", "attendees"):
        value = arguments.get(key)
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, list):
            found.extend(str(item) for item in cast("list[Any]", value))
    return found


class MailBox:
    """The owner's inbox, as far as the adapter's own notices go. The agent has no access to it."""

    def __init__(self) -> None:
        self.notices: list[HeldNotice] = []

    def held(self, notice: HeldNotice) -> None:
        self.notices.append(notice)
