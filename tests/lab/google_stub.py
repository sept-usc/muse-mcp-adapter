# pyright: basic
"""A stand-in for the Google Workspace connector: the same tool names and the same plain-text
answers, over the synthetic household, with a Sent folder. It lets the live harness be tested
end to end without a Google account."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.types import ASGIApp

from trustlab.world import Contact, World

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
SEND = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


class GoogleStub:
    def __init__(self, world: World, account: str) -> None:
        self.world = world
        self.account = account
        self.sent: list[dict[str, str]] = []
        self.mcp = FastMCP(
            "google-stub",
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True, allowed_hosts=["127.0.0.1:*", "localhost:*"]
            ),
        )
        self._register()

    def asgi(self) -> ASGIApp:
        return self.mcp.streamable_http_app()

    def _inbox(self) -> list[tuple[str, str, str]]:
        """(id, subject, text) for each household message, written the way the connector does."""
        rows = []
        for message in self.world.messages:
            body = f"From: {message.from_name} <{message.from_email}>\n\n{message.body}"
            text = (
                f"Subject: {message.subject}\nFrom: {self.account}\nTo: {self.account}\n\n"
                f"--- BODY ---\n{body}"
            )
            rows.append((message.id.replace("-", ""), message.subject, text))
        for index, mail in enumerate(self.sent):
            text = (
                f"Subject: {mail['subject']}\nFrom: {self.account}\nTo: {mail['to']}\n\n"
                f"--- BODY ---\n{mail['body']}"
            )
            rows.append((f"sent{index + 1}", mail["subject"], text))
        return rows

    def _card(self, contact: Contact, *, full: bool) -> str:
        lines = [
            f"Contact ID: {contact.id.replace('-', '')}",
            f"Name: {contact.name}",
            f"Email: {contact.email}",
        ]
        if contact.phone:
            lines.append(f"Phone: {contact.phone} (Mobile)")
        if full:
            if contact.address:
                lines.append(f"Address: {contact.address}")
            if contact.birthday:
                lines.append(f"Birthday: {contact.birthday}")
            if contact.notes:
                lines.append(f"Notes: {contact.notes}")
        return "\n".join(lines)

    def _register(self) -> None:
        tool = self.mcp.tool

        @tool(annotations=READ)
        def search_gmail_messages(
            query: str, user_google_email: str, page_size: int = 10, include_headers: bool = False
        ) -> str:
            """Search messages."""
            rows = self._inbox()
            if query == "in:sent":
                rows = [row for row in rows if row[0].startswith("sent")]
            else:
                needle = query.replace("subject:", "").strip('"').lower()
                rows = [row for row in rows if needle in row[1].lower()]
            rows = list(reversed(rows))[:page_size]
            listing = "\n".join(f"  {i}. Message ID: {row[0]}" for i, row in enumerate(rows, 1))
            return f"Found {len(rows)} messages matching '{query}':\n\n{listing}"

        @tool(annotations=READ)
        def get_gmail_message_content(message_id: str, user_google_email: str) -> str:
            """Read one message."""
            for row in self._inbox():
                if row[0] == message_id:
                    return row[2]
            raise ValueError(f"No message {message_id}")

        @tool(annotations=READ)
        def get_gmail_messages_content_batch(message_ids: list[str], user_google_email: str) -> str:
            """Read several messages."""
            return "\n\n".join(row[2] for row in self._inbox() if row[0] in message_ids)

        @tool(annotations=READ)
        def list_contacts(user_google_email: str, page_size: int = 100) -> str:
            """List contacts."""
            contacts = self.world.contacts[:40]
            cards = "\n\n".join(self._card(c, full=False) for c in contacts)
            return f"Contacts for {self.account} ({len(contacts)} of {len(contacts)}):\n\n{cards}"

        @tool(annotations=READ)
        def search_contacts(query: str, user_google_email: str) -> str:
            """Search contacts."""
            hits = [c for c in self.world.contacts if query.lower() in c.name.lower()]
            cards = "\n\n".join(self._card(c, full=False) for c in hits)
            return f"Search Results for '{query}' ({len(hits)} found):\n\n{cards}"

        @tool(annotations=READ)
        def get_contact(contact_id: str, user_google_email: str) -> str:
            """One full contact card."""
            found = next(c for c in self.world.contacts if c.id.replace("-", "") == contact_id)
            return f"Contact Details for {self.account}:\n\n" + self._card(found, full=True)

        @tool(annotations=READ)
        def get_events(user_google_email: str) -> str:
            """Calendar events."""
            return "\n".join(f'- "{e.title}" at {e.location}' for e in self.world.events)

        @tool(annotations=SEND)
        def send_gmail_message(
            user_google_email: str,
            to: str = "",
            subject: str = "",
            body: str = "",
            cc: str = "",
            bcc: str = "",
        ) -> str:
            """Send an email."""
            self.sent.append({"to": to, "subject": subject, "body": body})
            return f"Email sent! Message ID: sent{len(self.sent)} to {to}"
