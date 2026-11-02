"""The pages where the owner reviews and approves what an agent asked for.

The link the agent hands over only opens the page. Nothing happens until the owner types their
code into it, in a browser the agent does not control, and every change is bound to a digest of
what the page showed. Text from the agent or the server is always escaped and always labelled.
"""

from __future__ import annotations

import hmac
import html
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import cast
from urllib.parse import parse_qs, urlsplit

import anyio
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from ..application.oauth_web import CALLBACK_PATH
from ..application.web_pages import document, redirect
from ..domain.errors import (
    AuthorizationError,
    ControlPlaneError,
    ReauthorizationRequiredError,
)
from .compile import Candidate, Computed
from .models import (
    APPLIED,
    CONNECTING,
    DECLINED,
    EXPIRED,
    FAILED,
    IN_REVIEW,
    KIND_CHANGE,
    WAITING,
)
from .service import ProposalInfo, Revealed, SetupService
from .store import Proposal

SESSION_COOKIE = "muse_adapter_owner"
CSRF_COOKIE = "muse_adapter_owner_csrf"
PATH = "/review"
MAX_FORM_BYTES = 128 * 1024

_LABEL = {
    "read": ("Reads only", "r"),
    "write": ("Changes data", "w"),
    "sensitive_write": ("Changes data, may be hard to undo", "s"),
}
_KIND = {
    "tool": "operation",
    "resource": "resource",
    "resource_template": "resource",
    "prompt": "prompt",
}
_STANDING_LIMITS = (
    "The adapter does not yet ask you before each call. It does limit which values an operation "
    "may be called with, where a limit is shown under the operation, and it ends access at the "
    "time shown above, if you asked for that. Anything else you asked for in words is listed "
    "here as not enforced."
)


@dataclass(slots=True)
class _Context:
    token: str
    proposal: Proposal
    form: dict[str, list[str]]


class OwnerWeb:
    def __init__(self, *, service: SetupService, public_url: str) -> None:
        self._service = service
        self._public_url = public_url.rstrip("/")
        self._host = urlsplit(public_url).hostname or public_url
        self._secure = public_url.startswith("https://")

    def routes(self) -> list[Route]:
        return [
            Route(PATH + "/{token}", self._show, methods=["GET"]),
            Route(PATH + "/{token}/unlock", self._unlock, methods=["POST"]),
            Route(PATH + "/{token}/start", self._start, methods=["POST"]),
            Route(PATH + "/{token}/discover", self._discover, methods=["POST"]),
            Route(PATH + "/{token}/cancel", self._cancel, methods=["POST"]),
            Route(PATH + "/{token}/decide", self._decide, methods=["POST"]),
            Route(PATH + "/{token}/collected", self._collected, methods=["POST"]),
        ]

    # --- pages ---------------------------------------------------------------------------

    async def _show(self, request: Request) -> Response:
        token = request.path_params["token"]
        proposal = await self._run(lambda: self._service.lookup(token))
        if proposal is None:
            return self._message("Link not valid", "This review link is not valid.", 404)
        unlocked = self._service.session_valid(request.cookies.get(SESSION_COOKIE))
        # Why a request stopped can hold a server's words, so only the owner is told.
        closed = _closed(proposal, detail=unlocked)
        if closed is not None and (unlocked or proposal.state != FAILED):
            return self._message(*closed)
        if not unlocked:
            stopped = "This request stopped. Enter your owner code to see why."
            return self._unlock_page(proposal, token, intro=stopped if closed else None)
        return await self._state_page(proposal, token)

    async def _state_page(
        self, proposal: Proposal, token: str, error: str | None = None
    ) -> Response:
        info = await self._run(lambda: self._service.describe(proposal))
        if proposal.state == WAITING:
            return self._with_csrf(self._connect_page(proposal, info, token, error))
        if proposal.state == CONNECTING:
            return self._with_csrf(self._resume_page(proposal, info, token, error))
        if proposal.state == IN_REVIEW and proposal.computed is not None:
            computed = Computed.from_json(proposal.computed)
            return self._with_csrf(self._review_page(proposal, info, computed, token, error))
        if proposal.state == APPLIED:
            revealed = await self._run(lambda: self._service.reveal(proposal))
            operations = await self._run(
                lambda: self._service.operations(str(proposal.target_server_id))
            )
            return self._with_csrf(self._done_page(proposal, info, revealed, operations, token))
        return self._message("One moment", "This is being put in place. Reload in a few seconds.")

    # --- actions -------------------------------------------------------------------------

    async def _unlock(self, request: Request) -> Response:
        context = await self._context(request, need_session=False)
        if isinstance(context, Response):
            return context
        code = _first(context.form, "code")
        try:
            secret = await self._run(lambda: self._service.unlock(context.proposal, code))
        except AuthorizationError as error:
            return self._message("Locked", str(error), 429)
        except ControlPlaneError as error:
            return self._message("Not set up", str(error), 409)
        if secret is None:
            return self._unlock_page(
                context.proposal, context.token, "That code is not right.", status=403
            )
        response = redirect(f"{PATH}/{context.token}")
        response.set_cookie(
            SESSION_COOKIE,
            secret,
            max_age=1800,
            httponly=True,
            secure=self._secure,
            samesite="lax",
            path=PATH,
        )
        return response

    async def _start(self, request: Request) -> Response:
        context = await self._context(request)
        if isinstance(context, Response):
            return context
        return_path = f"{PATH}/{context.token}"
        try:
            target = await self._run(
                lambda: self._service.start(
                    context.proposal,
                    plan=_first(context.form, "plan") or None,
                    secret=_first(context.form, "secret") or None,
                    return_path=return_path,
                    client_id=_first(context.form, "client_id") or None,
                    client_secret=_first(context.form, "client_secret") or None,
                )
            )
            if target is not None:
                return redirect(target)
            fresh = await self._reload(context)
            if fresh.state == CONNECTING:  # a second press finds it already looked at
                await self._run(lambda: self._service.discover(fresh))
        except Exception as error:  # a page must say what stopped, never fail with a trace
            return await self._failed(context, error)
        return redirect(return_path)

    async def _discover(self, request: Request) -> Response:
        context = await self._context(request)
        if isinstance(context, Response):
            return context
        try:
            await self._run(lambda: self._service.discover(context.proposal))
        except Exception as error:  # a page must say what stopped, never fail with a trace
            return await self._failed(context, error)
        return redirect(f"{PATH}/{context.token}")

    async def _cancel(self, request: Request) -> Response:
        context = await self._context(request)
        if isinstance(context, Response):
            return context
        try:
            await self._run(lambda: self._service.cancel(context.proposal))
        except Exception as error:  # a page must say what stopped, never fail with a trace
            return await self._failed(context, error)
        return redirect(f"{PATH}/{context.token}")

    async def _decide(self, request: Request) -> Response:
        context = await self._context(request)
        if isinstance(context, Response):
            return context
        form = context.form
        try:
            await self._run(
                lambda: self._service.decide(
                    context.proposal,
                    approve=_first(form, "action") == "approve",
                    shown_digest=_first(form, "digest"),
                    chosen=form.get("chosen", []),
                    reviewed_read=form.get("read", []),
                )
            )
        except Exception as error:  # a page must say what stopped, never fail with a trace
            return await self._failed(context, error)
        return redirect(f"{PATH}/{context.token}")

    async def _collected(self, request: Request) -> Response:
        context = await self._context(request)
        if isinstance(context, Response):
            return context
        await self._run(lambda: self._service.collected(context.proposal))
        return redirect(f"{PATH}/{context.token}")

    async def _failed(self, context: _Context, error: Exception) -> Response:
        message = _plain(error)
        fresh = await self._reload(context)
        closed = _closed(fresh, detail=True)
        if closed is not None:
            return self._message(closed[0], f"{closed[1]} {message}", closed[2])
        if isinstance(error, ReauthorizationRequiredError):
            message = "You are not signed in to the service yet. Sign in, then try again."
        return await self._state_page(fresh, context.token, message)

    async def _reload(self, context: _Context) -> Proposal:
        fresh = await self._run(lambda: self._service.lookup(context.token))
        return fresh if fresh is not None else context.proposal

    # --- shared plumbing -----------------------------------------------------------------

    async def _context(self, request: Request, *, need_session: bool = True) -> _Context | Response:
        token = request.path_params["token"]
        proposal = await self._run(lambda: self._service.lookup(token))
        if proposal is None:
            return self._message("Link not valid", "This review link is not valid.", 404)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_FORM_BYTES:
            return self._message("Too large", "That request is too large.", 413)
        body = await request.body()
        if len(body) > MAX_FORM_BYTES:
            return self._message("Too large", "That request is too large.", 413)
        form = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
        csrf = _first(form, "csrf")
        cookie = request.cookies.get(CSRF_COOKIE, "")
        if not csrf or not hmac.compare_digest(csrf, cookie):
            return self._message(
                "Request not accepted", "This form has expired. Open the link again.", 403
            )
        if need_session and not self._service.session_valid(request.cookies.get(SESSION_COOKIE)):
            return redirect(f"{PATH}/{token}")
        return _Context(token, proposal, form)

    @staticmethod
    async def _run[T](function: Callable[[], T]) -> T:
        return await anyio.to_thread.run_sync(function)

    def _with_csrf(self, page: tuple[str, str, int]) -> Response:
        """Wrap a form page: a fresh cross-site token in the page and in a cookie."""
        title, body, status = page
        csrf = secrets.token_urlsafe(24)
        response = document(title, body.replace("{{CSRF}}", html.escape(csrf)), status)
        response.set_cookie(
            CSRF_COOKIE,
            csrf,
            max_age=1800,
            httponly=True,
            secure=self._secure,
            samesite="lax",
            path=PATH,
        )
        return response

    def _bar(self) -> str:
        return f"<div class=bar>Your adapter at <b>{html.escape(self._host)}</b></div>"

    def _message(self, title: str, message: str, status: int = 200) -> Response:
        body = (
            f"{self._bar()}<main><h1>{html.escape(title)}</h1>"
            f"<div class=box>{html.escape(message)}</div></main>"
        )
        return document(title, body, status)

    def _unlock_page(
        self,
        proposal: Proposal,
        token: str,
        error: str | None = None,
        *,
        status: int = 200,
        intro: str | None = None,
    ) -> Response:
        if self._service.locked(proposal):
            return self._message(
                "Locked",
                "Too many wrong codes. This link is locked for a while. Ask for a new request.",
                429,
            )
        problem = f"<div class='box bad'>{html.escape(error)}</div>" if error else ""
        if intro:
            problem = f"<div class=box>{html.escape(intro)}</div>{problem}"
        body = (
            f"{self._bar()}<main><h1>Enter your owner code</h1>"
            "<p>Muse asked your adapter to set something up. To look at it, type the owner code "
            "that was shown on your own machine when chat setup was turned on. Only type it "
            f"here, on this address: <b>{html.escape(self._host)}</b>.</p>{problem}"
            f"<form method=post action='{PATH}/{html.escape(token)}/unlock'>"
            "<input type=hidden name=csrf value='{{CSRF}}'>"
            "<p><input type=password name=code autocomplete=off autocapitalize=characters "
            "autofocus aria-label='Owner code'></p><button class=go>Continue</button></form>"
            "</main>"
        )
        return self._with_csrf(("Enter your owner code", body, status))

    # --- page bodies ---------------------------------------------------------------------

    def _summary(self, proposal: Proposal, info: ProposalInfo) -> str:
        where = f"<b>{html.escape(info.name)}</b> at <code>{html.escape(info.host)}</code>" + (
            "" if info.listed else " <span class=tag>not in the adapter's list</span>"
        )
        words = html.escape(proposal.user_words)
        return (
            f"<div class='box quote'><div class=muted>What Muse says you asked for. Muse can be "
            f"wrong or tricked, so judge by the list further down, not by these words.</div>"
            f"<div>{words}</div></div><p>Service: {where}</p>"
        )

    def _connect_page(
        self, proposal: Proposal, info: ProposalInfo, token: str, error: str | None
    ) -> tuple[str, str, int]:
        problem = f"<div class='box bad'>{html.escape(error)}</div>" if error else ""
        action = f"{PATH}/{html.escape(token)}/start"
        if proposal.kind == KIND_CHANGE:
            title = "Look at the change Muse asked for"
            form = "<p>The adapter will look at what the service offers now.</p>"
            button = "Look at what it offers"
        elif info.plan == "none":
            title = "Connect a service"
            form = "<p>This service needs no sign-in.</p>"
            button = "Look at what it offers"
        elif info.plan == "oauth":
            title = "Connect a service"
            form = (
                f"<p>You will sign in on <b>{html.escape(info.host)}</b>'s own page. The adapter "
                "keeps the result; Muse never sees it.</p>"
            )
            button = "Sign in and continue"
        elif info.plan == "oauth_app":
            title = "Connect a service"
            form = self._own_app_form(info)
            button = "Save and sign in"
        elif info.plan == "env_token":
            title = "Connect a service"
            help_text = html.escape(info.token_help or "")
            form = (
                f"<p>This service needs a token. {help_text}</p>"
                f"<p><label>{html.escape(info.secret_label or 'Token')}<br>"
                "<input type=password name=secret autocomplete=off></label></p>"
                "<p class=muted>It is stored encrypted on your machine and never shown to Muse."
                "</p>"
            )
            button = "Save and look at what it offers"
        else:
            title = "Connect a service that is not in the list"
            form = (
                "<div class='box warn'><b>Muse supplied this address.</b> It is not in the "
                "adapter's own list. Only continue if you recognise "
                f"<code>{html.escape(info.host)}</code> and expect it.</div>"
                "<p>How does it sign in?</p>"
                "<p><label><input type=radio name=plan value=none checked> No sign-in</label><br>"
                "<label><input type=radio name=plan value=oauth> Sign in on its own page</label>"
                "<br><label><input type=radio name=plan value=oauth_app> Sign in on its own page, "
                "with an app I registered with the service</label>"
                "<br><label><input type=radio name=plan value=env_token> A token I paste "
                "here</label></p><p><label>Token, if you chose that<br>"
                "<input type=password name=secret autocomplete=off></label></p>"
                f"<p class=muted>If you chose an app, give its details. {self._app_steps(None)}</p>"
                f"{_app_fields()}"
            )
            button = "Continue"
        body = (
            f"{self._bar()}<main><h1>{title}</h1>{problem}{self._summary(proposal, info)}"
            f"<form method=post action='{action}'>"
            "<input type=hidden name=csrf value='{{CSRF}}'>"
            f"{form}<button class=go>{button}</button></form>"
            f"{self._cancel_form(token)}</main>"
        )
        return title, body, 200

    @staticmethod
    def _cancel_form(token: str) -> str:
        return (
            f"<form method=post action='{PATH}/{html.escape(token)}/cancel'>"
            "<input type=hidden name=csrf value='{{CSRF}}'>"
            "<p class=muted>Not what you wanted? <button>Cancel this request</button></p></form>"
        )

    def _app_steps(self, docs: str | None) -> str:
        """What the owner does at the service to get a client ID and secret."""
        callback = f"{self._public_url}{CALLBACK_PATH}"
        where = (
            f" See <a href='{html.escape(docs, quote=True)}' rel='noopener noreferrer'>"
            "the service's own instructions</a>."
            if docs and docs.startswith("https://")
            else ""
        )
        return (
            "Register an app in the service's developer settings. When it asks for a redirect "
            f"URL, give exactly <code>{html.escape(callback)}</code>. Then copy the app's client "
            f"ID and client secret here.{where}"
        )

    def _own_app_form(self, info: ProposalInfo) -> str:
        return (
            f"<p><b>{html.escape(info.name)}</b> only signs in through an app that you register "
            f"with them once, because it does not let other programs register themselves.</p>"
            f"<p>{self._app_steps(info.docs)}</p>{_app_fields()}"
            "<p class=muted>They are stored encrypted on your machine and never shown to Muse. "
            "You then sign in on the service's own page.</p>"
        )

    def _resume_page(
        self, proposal: Proposal, info: ProposalInfo, token: str, error: str | None
    ) -> tuple[str, str, int]:
        problem = f"<div class='box bad'>{html.escape(error)}</div>" if error else ""
        base = f"{PATH}/{html.escape(token)}"
        again = (
            f"<form method=post action='{base}/start'><input type=hidden name=csrf "
            "value='{{CSRF}}'><button>Sign in again</button></form>"
            if info.plan in ("oauth", "oauth_app")
            else ""
        )
        body = (
            f"{self._bar()}<main><h1>Almost there</h1>{problem}{self._summary(proposal, info)}"
            "<p>The connection is set up. Next the adapter looks at what the service offers.</p>"
            f"<form method=post action='{base}/discover'><input type=hidden name=csrf "
            "value='{{CSRF}}'><button class=go>Look at what it offers</button></form>"
            f"{again}{self._cancel_form(token)}</main>"
        )
        return "Almost there", body, 200

    def _review_page(
        self,
        proposal: Proposal,
        info: ProposalInfo,
        computed: Computed,
        token: str,
        error: str | None,
    ) -> tuple[str, str, int]:
        problem = f"<div class='box bad'>{html.escape(error)}</div>" if error else ""
        selected = [item for item in computed.candidates if item.selected]
        suggested = [item for item in computed.candidates if item.suggested]
        others = [item for item in computed.candidates if not item.selected]
        requested = set(computed.requested)
        chosen = {item.key for item in selected}
        extra = chosen - requested
        if proposal.kind == KIND_CHANGE:
            stays = len(extra)
            line = f"Asked for now: {len(chosen & requested)}. Already live and staying: {stays}."
            alignment = f"<div class='box ok'>{line}</div>"
        else:
            style = "ok" if not extra else "warn"
            alignment = (
                f"<div class='box {style}'>Asked for: {len(requested)}. Would be available: "
                f"{len(chosen)}. Available but not asked for: {len(extra)}.</div>"
            )
        access = _access_box(computed)
        rows = "".join(_row(item, checked=True) for item in selected) or (
            "<tr><td colspan=3 class=muted>Nothing would become available.</td></tr>"
        )
        suggestion_rows = "".join(_row(item, checked=False) for item in suggested)
        suggestions = (
            "<h2>These look read-only but the server did not say so</h2><table>"
            f"{suggestion_rows}</table>"
            if suggested
            else ""
        )
        remaining = "".join(_row(item, checked=False) for item in others if not item.suggested)
        rest = (
            f"<details><summary>Everything else the service offers ({len(others)}), not "
            f"available. Tick to add.</summary><table>{remaining}</table></details>"
            if remaining
            else ""
        )
        notes: list[str] = [f"<li>{html.escape(item)}</li>" for item in computed.unenforced]
        notes.append(f"<li>{html.escape(_STANDING_LIMITS)}</li>")
        unmatched = "".join(f"<li>{html.escape(item)}</li>" for item in computed.unresolved)
        gone = "".join(f"<li>{html.escape(item)}</li>" for item in computed.gone)
        sections = (f"<h2>Could not be matched</h2><ul>{unmatched}</ul>" if unmatched else "") + (
            f"<h2>No longer offered, so dropped</h2><ul>{gone}</ul>" if gone else ""
        )
        base = f"{PATH}/{html.escape(token)}"
        body = (
            f"{self._bar()}<main><h1>Review: {html.escape(info.name)}</h1>{problem}"
            f"{self._summary(proposal, info)}{alignment}{access}"
            "<form method=post action='" + base + "/decide'>"
            "<input type=hidden name=csrf value='{{CSRF}}'>"
            f"<input type=hidden name=digest value='{html.escape(computed.digest)}'>"
            "<h2>What would become available to Muse</h2>"
            "<p class=muted>Untick anything you do not want. Only what stays ticked is "
            "available.</p>"
            f"<table>{rows}</table>{suggestions}{rest}"
            f"<h2>What the adapter does not enforce</h2><div class='box warn'><ul>{''.join(notes)}"
            f"</ul></div>{sections}"
            "<p><button class=go name=action value=approve>Approve</button> "
            "<button name=action value=decline>Decline</button></p></form></main>"
        )
        return "Review", body, 200

    def _done_page(
        self,
        proposal: Proposal,
        info: ProposalInfo,
        revealed: Revealed | None,
        operations: list[dict[str, object]],
        token: str,
    ) -> tuple[str, str, int]:
        listed = "".join(
            f"<li><code>{html.escape(str(item['id']))}</code> "
            f"{html.escape(_LABEL.get(str(item['kind']), ('', ''))[0])}{_limited_to(item)}</li>"
            for item in operations
        )
        if proposal.kind == KIND_CHANGE:
            access = (
                "<div class=box>The connector, its address and its token stay as they were. If "
                "Muse still lists the old operations, ask it to refresh the connector.</div>"
            )
        elif revealed is None:
            access = (
                "<div class=box>The connector token was shown here and has been cleared. If you "
                "did not copy it, make a new one at the command line with "
                "<code>muse-vm rotate-token</code>.</div>"
            )
        else:
            item = revealed
            code = (
                f"<p>Or sign in with the connection code <code>{html.escape(item.connection_code)}"
                "</code>.</p>"
                if item.connection_code
                else ""
            )
            access = (
                "<h2>Give this to Muse</h2><div class='box warn'>"
                f"<p>Connector address:<br><code>{html.escape(item.api_description_url)}</code></p>"
                f"<p>Connector token (shown only here):<br><code>{html.escape(item.token)}</code>"
                f"</p>{code}<p class=muted>In Muse, create a connector from that address, click "
                "Connect, and paste the token into Muse's own secure field. Do not type it into "
                "the chat.</p></div>"
                f"<form method=post action='{PATH}/{html.escape(token)}/collected'>"
                "<input type=hidden name=csrf value='{{CSRF}}'>"
                "<button>I have saved the token. Clear it from here.</button></form>"
            )
        ends = sorted(str(item["accessEndsAt"]) for item in operations if item.get("accessEndsAt"))
        until = (
            f"<div class='box warn'>Access ends at {html.escape(ends[0])}. After that Muse can no "
            "longer use it.</div>"
            if ends
            else ""
        )
        body = (
            f"{self._bar()}<main><h1>Approved: {html.escape(info.name)}</h1>"
            "<div class='box ok'>It is in place. Muse can use only what is listed below.</div>"
            f"{until}<h2>Available to Muse</h2><ul>{listed or '<li>nothing</li>'}</ul>{access}"
            "</main>"
        )
        return "Approved", body, 200


def _access_box(computed: Computed) -> str:
    """Whether the access is temporary, and when it ends."""
    hours = computed.expires_in_hours
    if hours:
        return (
            f"<div class='box warn'>Access is temporary: it ends {hours} hour"
            f"{'' if hours == 1 else 's'} after you approve. After that Muse can no longer "
            "use it.</div>"
        )
    if computed.inherited_expiry and computed.inherited_ended:
        return (
            f"<div class='box warn'>Access to this connection ended at "
            f"{html.escape(computed.inherited_expiry)}. Whatever you approve now, without a new "
            "time, has ended already. To use it again, ask Muse for a time, for example five more "
            "hours.</div>"
        )
    if computed.inherited_expiry:
        return (
            f"<div class=box>Access ends at {html.escape(computed.inherited_expiry)}, as it did "
            "before. Anything you add ends then too.</div>"
        )
    return ""


def _app_fields() -> str:
    return (
        "<p><label>Client ID<br>"
        "<input name=client_id autocomplete=off spellcheck=false></label></p>"
        "<p><label>Client secret (leave empty if the service gave none)<br>"
        "<input type=password name=client_secret autocomplete=off></label></p>"
    )


def _limited_to(operation: Mapping[str, object]) -> str:
    raw = operation.get("limits")
    if not isinstance(raw, Mapping) or not raw:
        return ""
    shown = "; ".join(
        f"{html.escape(str(name))} {html.escape(str(words))}"
        for name, words in cast(Mapping[object, object], raw).items()
    )
    return f" <span class=muted>(only {shown})</span>"


def _row(item: Candidate, *, checked: bool) -> str:
    label, shade = _LABEL.get(item.classification, (item.classification, ""))
    box = "checked" if checked else ""
    key = html.escape(item.key, quote=True)
    note = html.escape(item.evidence)
    if item.changed:
        note += " The server changed this since it was approved."
    review = (
        f"<label class=muted><input type=checkbox name=read value='{key}'> treat as read-only "
        "(I checked)</label>"
        if item.reviewable and checked
        else ""
    )
    limited = (
        "<div class=muted>Limited by you to: "
        + "; ".join(
            f"<b>{html.escape(name)}</b> {html.escape(words)}" for name, words in item.limits
        )
        + "</div>"
        if item.limits
        else ""
    )
    detail = (
        "<details><summary class=muted>The server's own description (not verified)</summary>"
        f"<div class=muted>{html.escape(item.description)}</div></details>"
        if item.description
        else ""
    )
    return (
        f"<tr><td><input type=checkbox name=chosen value='{key}' {box} "
        f"aria-label='{html.escape(item.name, quote=True)}'></td>"
        f"<td><b>{html.escape(item.name)}</b> <span class=tag>"
        f"{html.escape(_KIND.get(item.kind, item.kind))}</span>"
        f"<div class=muted>{note}</div>{limited}{detail}</td>"
        f"<td><span class='tag {shade}'>{html.escape(label)}</span><br>{review}</td></tr>"
    )


def _closed(proposal: Proposal, *, detail: bool) -> tuple[str, str, int] | None:
    if proposal.state == DECLINED:
        return "Declined", "This request was declined.", 410
    if proposal.state == EXPIRED:
        return "Expired", "This request expired. Ask Muse to try again.", 410
    if proposal.state == FAILED:
        reason = (proposal.error if detail else None) or "It could not be completed."
        return "Stopped", reason, 409
    return None


def _first(form: Mapping[str, list[str]], name: str) -> str:
    values = form.get(name)
    return values[0] if values else ""


def _plain(error: Exception) -> str:
    text = " ".join(str(error).split())
    return text[:300] or type(error).__name__
