"""The page where the owner approves or declines a held action.

The link in the chat is a one-time token, and the agent can see it. So the token only opens the
page; deciding needs the owner's approval passcode, which the owner types here and nowhere else,
and which is never in the agent's context. A wrong passcode is counted, and after a few the page
locks for a while.
"""

from __future__ import annotations

import html
import json
from collections.abc import Callable
from urllib.parse import parse_qs

import anyio
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from ..domain.errors import ConflictError
from ..persistence.owner_store import APPROVED, DENIED, PENDING, ApprovalRequest, OwnerStore
from .web_pages import page

MAX_FORM_BYTES = 4096
MAX_SHOWN_CHARS = 6000
PATH = "/approve"
NOT_WAITING = "This request is no longer waiting for a decision."


class ApprovalWeb:
    def __init__(self, *, store: OwnerStore, clock: Callable[[], str], public_url: str) -> None:
        self._store = store
        self._clock = clock
        self._public_url = public_url.rstrip("/")

    def link(self, token: str) -> str:
        return f"{self._public_url}{PATH}/{token}"

    def routes(self) -> list[Route]:
        return [Route(PATH + "/{token}", self._handle, methods=["GET", "POST"])]

    async def _handle(self, request: Request) -> Response:
        token = request.path_params["token"]
        if request.method == "GET":
            return await anyio.to_thread.run_sync(lambda: self._show(token))
        body = await request.body()
        if len(body) > MAX_FORM_BYTES:
            return page("Too large", "That request is too large.", 413)
        form = {key: values[0] for key, values in parse_qs(body.decode("utf-8", "replace")).items()}
        return await anyio.to_thread.run_sync(lambda: self._decide(token, form))

    def _show(self, token: str) -> Response:
        pending = self._store.by_token(token)
        if pending is None:
            return page("Link not found", "This approval link is not valid.", 404)
        now = self._clock()
        if pending.status != PENDING:
            return page("Already decided", _decided(pending))
        if pending.expired(now):
            return page(
                "Link expired",
                "This approval link has expired. Ask the assistant to try the action again.",
                410,
            )
        return page(
            "Approve this action?", "", extra=_form(pending, locked=self._store.locked(now))
        )

    def _decide(self, token: str, form: dict[str, str]) -> Response:
        pending = self._store.by_token(token)
        now = self._clock()
        if pending is None:
            return page("Link not found", "This approval link is not valid.", 404)
        if pending.status != PENDING or pending.expired(now):
            return page(
                "No longer waiting", "This request is no longer waiting for a decision.", 410
            )
        if self._store.locked(now):
            return page(
                "Too many wrong passcodes",
                "Approvals are paused for a few minutes. Try again later, or use the command line.",
                429,
            )
        if not self._store.check_passcode(form.get("passcode", ""), now):
            return page(
                "Wrong passcode",
                "That passcode was not accepted. Go back and try again.",
                403,
            )
        approve = form.get("decision") == "approve"
        try:
            self._store.decide(pending.request_id, approve=approve, by="owner-page", now=now)
        except ConflictError:
            return page(
                "No longer waiting", "This request is no longer waiting for a decision.", 410
            )
        if approve and form.get("remember") == "yes" and pending.recipients:
            self._store.remember_recipients(pending.connection_id, pending.recipients, now)
        if approve:
            return page(
                "Approved",
                "Done. Tell the assistant you approved it; it can now run this one action.",
            )
        return page("Declined", "Done. The action will not run.")


def _decided(item: ApprovalRequest) -> str:
    if item.status == APPROVED:
        return "This action was approved and is waiting to run."
    if item.status == DENIED:
        return "This action was declined."
    return "This action was approved and has already run."


def _form(item: ApprovalRequest, *, locked: bool) -> str:
    esc = html.escape
    shown = json.dumps(item.arguments, indent=2, sort_keys=True, ensure_ascii=False)
    if len(shown) > MAX_SHOWN_CHARS:
        shown = shown[:MAX_SHOWN_CHARS] + "\n... (cut here; the rest is not shown)"
    reasons = "".join(f"<li>{esc(reason)}</li>" for reason in item.reasons)
    remember = (
        "<p><label><input type=checkbox name=remember value=yes> Do not ask again for "
        f"{esc(', '.join(item.recipients))}</label></p>"
        if item.recipients
        else ""
    )
    notice = (
        "<p><b>Approvals are paused after too many wrong passcodes. Try again later.</b></p>"
        if locked
        else ""
    )
    return (
        f"<p>The assistant wants to run <b>{esc(item.public_operation_id)}</b> "
        f"({esc(item.classification.replace('_', ' '))}).</p>"
        f"<h3>Why you are being asked</h3><ul>{reasons}</ul>"
        "<h3>Exactly what it will do</h3>"
        "<pre style='white-space:pre-wrap;word-break:break-word;background:#f4f4f4;"
        f"padding:.8rem;border-radius:6px'>{esc(shown)}</pre>"
        f"<form method=post>{notice}"
        "<p><label>Your approval passcode<br>"
        "<input type=password name=passcode autocomplete=off required "
        "style='width:100%;padding:.5rem'></label></p>"
        f"{remember}"
        "<p><button name=decision value=approve style='padding:.6rem 1.2rem'>Approve</button> "
        "<button name=decision value=decline style='padding:.6rem 1.2rem'>Decline</button></p>"
        "</form>"
        "<p style='color:#666'>Only enter your passcode here, on this page. The assistant never "
        "needs it.</p>"
    )
