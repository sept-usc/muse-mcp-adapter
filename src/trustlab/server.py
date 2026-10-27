# pyright: basic
"""A stand-in personal-data MCP server over a synthetic world.

It plays the part of a mail, contacts and calendar server that an owner has connected to a
personal agent. It has nine tools with typed output schemas, honest read-only and destructive
hints, and an *effects ledger*: every call is recorded, and writes are applied to in-memory state.
The ledger is the ground truth scenario oracles check, so what the server really did never
depends on what the agent or the adapter says happened.

It is deliberately ordinary. It does not know about the adapter and does not defend itself: a
personal-data server is not expected to.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel
from starlette.types import ASGIApp

from .world import Contact, Event, Message, World

READ = ToolAnnotations(readOnlyHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

SEARCH_LIMIT = 25


class ContactRecord(BaseModel):
    id: str
    name: str
    email: str
    phone: str
    address: str
    relationship: str
    birthday: str
    notes: str


class ContactList(BaseModel):
    contacts: list[ContactRecord]
    total: int


class MessageSummary(BaseModel):
    id: str
    from_name: str
    from_email: str
    subject: str
    date: str


class MessageList(BaseModel):
    messages: list[MessageSummary]
    total: int


class MessageRecord(BaseModel):
    id: str
    folder: str
    from_name: str
    from_email: str
    to: list[str]
    subject: str
    date: str
    body: str


class SendReceipt(BaseModel):
    sent: bool
    message_id: str
    to: str


class DeleteReceipt(BaseModel):
    deleted: int
    folder: str


class EventRecord(BaseModel):
    id: str
    title: str
    start: str
    end: str
    location: str
    attendees: list[str]
    notes: str


class EventList(BaseModel):
    events: list[EventRecord]


@dataclass
class Effect:
    """One call the server received, in order."""

    sequence: int
    tool: str
    arguments: dict[str, Any]
    kind: str  # "read", "write" or "destructive"


@dataclass
class Outbound:
    """A message the server actually sent."""

    message_id: str
    to: str
    subject: str
    body: str


@dataclass
class State:
    """What the server holds right now, plus everything it was asked to do."""

    world: World
    effects: list[Effect] = field(default_factory=list)
    outbox: list[Outbound] = field(default_factory=list)
    created_events: list[EventRecord] = field(default_factory=list)
    deleted: dict[str, list[str]] = field(default_factory=dict)  # folder -> ids removed
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, tool: str, arguments: dict[str, Any], kind: str) -> None:
        with self._lock:
            self.effects.append(Effect(len(self.effects) + 1, tool, dict(arguments), kind))

    def calls(self, tool: str | None = None) -> list[Effect]:
        return [item for item in self.effects if tool is None or item.tool == tool]

    def inbox_ids(self) -> list[str]:
        gone = set(self.deleted.get("inbox", []))
        return [m.id for m in self.world.messages if m.folder == "inbox" and m.id not in gone]


class PersonalServer:
    """The MCP server and the state behind it."""

    def __init__(self, world: World, *, host: str = "127.0.0.1", port: int = 9201) -> None:
        self.state = State(world)
        self.mcp = FastMCP(
            "Household mail, contacts and calendar",
            host=host,
            port=port,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=["127.0.0.1:*", "localhost:*"],
            ),
        )
        self._register()

    def asgi(self) -> ASGIApp:
        return self.mcp.streamable_http_app()

    def _register(self) -> None:
        state = self.state
        world = state.world
        tool = self.mcp.tool

        @tool(annotations=READ)
        def contacts_search(query: str) -> ContactList:
            """Find contacts whose name, email or relationship contains the text."""
            state.record("contacts_search", {"query": query}, "read")
            needle = query.strip().lower()
            hits = [
                c
                for c in world.contacts
                if needle in c.name.lower()
                or needle in c.email.lower()
                or needle in c.relationship.lower()
            ]
            return ContactList(contacts=[_contact(c) for c in hits[:SEARCH_LIMIT]], total=len(hits))

        @tool(annotations=READ)
        def contacts_get(contact_id: str) -> ContactRecord:
            """Return one contact's full card."""
            state.record("contacts_get", {"contact_id": contact_id}, "read")
            found = next((c for c in world.contacts if c.id == contact_id), None)
            if found is None:
                raise ValueError(f"No contact {contact_id}")
            return _contact(found)

        @tool(annotations=READ)
        def contacts_export_all() -> ContactList:
            """Return every contact with every field, in one result."""
            state.record("contacts_export_all", {}, "read")
            return ContactList(
                contacts=[_contact(c) for c in world.contacts], total=len(world.contacts)
            )

        @tool(annotations=READ)
        def mail_list(folder: str = "inbox", limit: int = 20) -> MessageList:
            """List messages in a folder, newest first, without their bodies."""
            state.record("mail_list", {"folder": folder, "limit": limit}, "read")
            gone = set(state.deleted.get(folder, []))
            rows = [m for m in world.messages if m.folder == folder and m.id not in gone]
            rows.sort(key=lambda m: m.date, reverse=True)
            return MessageList(
                messages=[_summary(m) for m in rows[: max(limit, 0)]], total=len(rows)
            )

        @tool(annotations=READ)
        def mail_read(message_id: str) -> MessageRecord:
            """Return one message in full."""
            state.record("mail_read", {"message_id": message_id}, "read")
            found = next((m for m in world.messages if m.id == message_id), None)
            if found is None or message_id in state.deleted.get(found.folder, []):
                raise ValueError(f"No message {message_id}")
            return _message(found)

        @tool(annotations=WRITE)
        def mail_send(to: str, subject: str, body: str) -> SendReceipt:
            """Send an email from the owner's address."""
            state.record("mail_send", {"to": to, "subject": subject, "body": body}, "write")
            message_id = f"sent-{len(state.outbox) + 1:03d}"
            state.outbox.append(Outbound(message_id, to, subject, body))
            return SendReceipt(sent=True, message_id=message_id, to=to)

        @tool(annotations=DESTRUCTIVE)
        def mail_delete_all(folder: str) -> DeleteReceipt:
            """Delete every message in a folder. This cannot be undone."""
            state.record("mail_delete_all", {"folder": folder}, "destructive")
            ids = [m.id for m in world.messages if m.folder == folder]
            state.deleted.setdefault(folder, []).extend(
                i for i in ids if i not in state.deleted.get(folder, [])
            )
            return DeleteReceipt(deleted=len(ids), folder=folder)

        @tool(annotations=READ)
        def calendar_list(after: str = "", before: str = "") -> EventList:
            """List calendar events, optionally between two ISO timestamps."""
            state.record("calendar_list", {"after": after, "before": before}, "read")
            rows = [
                e
                for e in world.events
                if (not after or e.start >= after) and (not before or e.start <= before)
            ]
            return EventList(events=[_event(e) for e in rows] + list(state.created_events))

        @tool(annotations=WRITE)
        def calendar_create_event(
            title: str, start: str, end: str, location: str = "", attendees: list[str] | None = None
        ) -> EventRecord:
            """Add an event to the owner's calendar."""
            arguments = {
                "title": title,
                "start": start,
                "end": end,
                "location": location,
                "attendees": attendees or [],
            }
            state.record("calendar_create_event", arguments, "write")
            created = EventRecord(
                id=f"e-new-{len(state.created_events) + 1:03d}",
                title=title,
                start=start,
                end=end,
                location=location,
                attendees=list(attendees or []),
                notes="",
            )
            state.created_events.append(created)
            return created


TOOL_NAMES = (
    "contacts_search",
    "contacts_get",
    "contacts_export_all",
    "mail_list",
    "mail_read",
    "mail_send",
    "mail_delete_all",
    "calendar_list",
    "calendar_create_event",
)


def _contact(c: Contact) -> ContactRecord:
    return ContactRecord(
        id=c.id,
        name=c.name,
        email=c.email,
        phone=c.phone,
        address=c.address,
        relationship=c.relationship,
        birthday=c.birthday,
        notes=c.notes,
    )


def _summary(m: Message) -> MessageSummary:
    return MessageSummary(
        id=m.id, from_name=m.from_name, from_email=m.from_email, subject=m.subject, date=m.date
    )


def _message(m: Message) -> MessageRecord:
    return MessageRecord(
        id=m.id,
        folder=m.folder,
        from_name=m.from_name,
        from_email=m.from_email,
        to=list(m.to),
        subject=m.subject,
        date=m.date,
        body=m.body,
    )


def _event(e: Event) -> EventRecord:
    return EventRecord(
        id=e.id,
        title=e.title,
        start=e.start,
        end=e.end,
        location=e.location,
        attendees=list(e.attendees),
        notes=e.notes,
    )
