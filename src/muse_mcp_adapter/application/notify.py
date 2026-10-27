"""Tell the owner, by a route the agent is not on, that an action is waiting for them.

If the only way an owner learns of a held action is through the agent, an agent that hides it also
hides the need to approve. So the adapter sends its own message, with a link made for the owner
alone. The message names the tool and why it was held; it does not repeat the arguments, so
nothing about the action passes through the mail provider. The page shows the rest.

A failed notification never fails or delays the agent's call: it is recorded and the action simply
waits as before.
"""

from __future__ import annotations

import logging
import smtplib
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

log = logging.getLogger("muse_mcp_adapter.notify")


@dataclass(frozen=True, slots=True)
class HeldNotice:
    operation: str
    reasons: Sequence[str]
    link: str
    expires_at: str


class Notifier(Protocol):
    def held(self, notice: HeldNotice) -> None: ...


class SmtpNotifier:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        sender: str,
        to: str,
        username: str | None = None,
        password: str | None = None,
        starttls: bool = True,
        timeout: float = 10.0,
        background: bool = True,
        on_result: Callable[[bool, str], None] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._sender = sender
        self._to = to
        self._username = username
        self._password = password
        self._starttls = starttls
        self._timeout = timeout
        self._background = background
        self._on_result = on_result

    def held(self, notice: HeldNotice) -> None:
        message = _compose(self._sender, self._to, notice)
        if self._background:
            threading.Thread(target=self._send, args=(message,), daemon=True).start()
        else:
            self._send(message)

    def _send(self, message: EmailMessage) -> None:
        try:
            with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
                if self._starttls:
                    smtp.starttls()
                if self._username and self._password:
                    smtp.login(self._username, self._password)
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException) as error:
            log.warning("could not send the approval notice: %s", type(error).__name__)
            if self._on_result:
                self._on_result(False, type(error).__name__)
            return
        if self._on_result:
            self._on_result(True, "sent")


def _compose(sender: str, to: str, notice: HeldNotice) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = f"Waiting for your approval: {notice.operation}"
    why = "\n".join(f"  - {reason}" for reason in notice.reasons)
    message.set_content(
        f"Your assistant is waiting for your approval to run {notice.operation}.\n\n"
        f"Why you are being asked:\n{why}\n\n"
        "Open this link to see exactly what it will do, then approve or decline with your "
        f"approval passcode:\n{notice.link}\n\n"
        f"The link works until {notice.expires_at}. If you were not expecting this, decline it or "
        "ignore this message: nothing runs unless you approve it.\n"
    )
    return message
