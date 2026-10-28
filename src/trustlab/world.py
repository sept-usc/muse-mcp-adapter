"""The synthetic world every scenario runs in.

A world is plain data: an owner, contacts, mail and calendar events, and a registry of *planted*
values. A planted value is a sensitive string whose home is known (``contact:c-dana.notes``), so an
oracle can later ask a precise question: did this string reach the agent's context, and did it
leave through a write? Nothing here touches the network or the clock, and nothing is random: the
same file always gives the same world.

Safety rule, checked on load: every address is on a reserved test domain, so no scenario can ever
reach a real mailbox.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

WORLDS = Path(__file__).parent / "worlds"
DEFAULT_WORLD = "household_v0"

# RFC 2606 and RFC 6761 reserved names: nothing under them can be registered or delivered to.
_RESERVED_TLDS = frozenset({"test", "example", "invalid"})
_EMAIL = re.compile(r"^[^@\s]+@([^@\s]+)$")

_FIRST = ("Ada", "Ben", "Cleo", "Dev", "Esme", "Finn", "Gita", "Hugo", "Iris", "Jon", "Kira", "Leo")
_LAST = (
    "Abbott",
    "Bianchi",
    "Castro",
    "Dube",
    "Ellis",
    "Fischer",
    "Gupta",
    "Hale",
    "Ibarra",
    "Jensen",
    "Khan",
    "Lund",
    "Mori",
    "Nunez",
    "Okoye",
    "Pruitt",
    "Quinn",
    "Reyes",
    "Silva",
    "Tran",
)


class WorldError(ValueError):
    """The world file is malformed or breaks a safety rule."""


@dataclass(frozen=True)
class Contact:
    id: str
    name: str
    email: str
    phone: str
    address: str
    relationship: str
    birthday: str
    notes: str


@dataclass(frozen=True)
class Message:
    id: str
    folder: str
    from_name: str
    from_email: str
    to: tuple[str, ...]
    subject: str
    date: str
    body: str
    authored_by: str  # "third_party" or "owner": who wrote the text, whatever it says


@dataclass(frozen=True)
class Event:
    id: str
    title: str
    start: str
    end: str
    location: str
    attendees: tuple[str, ...]
    notes: str


@dataclass(frozen=True)
class Planted:
    name: str
    value: str
    kind: str
    where: str


@dataclass(frozen=True)
class Owner:
    name: str
    email: str
    phone: str


@dataclass(frozen=True)
class World:
    name: str
    description: str
    owner: Owner
    attacker_email: str
    contacts: tuple[Contact, ...]
    messages: tuple[Message, ...]
    events: tuple[Event, ...]
    planted: tuple[Planted, ...]
    named_contacts: int = field(default=0)  # how many contacts were written by hand

    def contact(self, contact_id: str) -> Contact:
        return next(item for item in self.contacts if item.id == contact_id)

    def message(self, message_id: str) -> Message:
        return next(item for item in self.messages if item.id == message_id)

    def planted_value(self, name: str) -> str:
        return next(item.value for item in self.planted if item.name == name)

    def planted_values(self) -> dict[str, str]:
        return {item.name: item.value for item in self.planted}

    def all_emails(self) -> set[str]:
        found = {self.owner.email, self.attacker_email}
        found.update(item.email for item in self.contacts)
        found.update(item.from_email for item in self.messages)
        for message in self.messages:
            found.update(message.to)
        for event in self.events:
            found.update(event.attendees)
        return found


def load_world(name: str = DEFAULT_WORLD, *, directory: Path = WORLDS) -> World:
    """Read ``<name>.json`` from the worlds directory, build it and check it."""
    path = directory / f"{name}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise WorldError(f"No world named {name!r} in {directory}") from error
    except json.JSONDecodeError as error:
        raise WorldError(f"{path.name} is not valid JSON: {error}") from error
    return build_world(raw)


def build_world(raw: dict[str, Any]) -> World:
    try:
        owner_raw = cast(dict[str, str], raw["owner"])
        hand_written = tuple(_contact(item) for item in cast(list[Any], raw["contacts"]))
        bulk = cast(dict[str, Any], raw.get("bulk_contacts") or {})
        contacts = hand_written + _bulk_contacts(
            int(bulk.get("count", 0)), str(bulk.get("domain", "directory.test"))
        )
        world = World(
            name=str(raw["name"]),
            description=str(raw.get("description", "")),
            owner=Owner(owner_raw["name"], owner_raw["email"], owner_raw["phone"]),
            attacker_email=str(cast(dict[str, Any], raw["attacker"])["email"]),
            contacts=contacts,
            messages=tuple(_message(item) for item in cast(list[Any], raw["messages"])),
            events=tuple(_event(item) for item in cast(list[Any], raw["events"])),
            planted=tuple(
                Planted(name, str(item["value"]), str(item["kind"]), str(item["where"]))
                for name, item in cast(dict[str, Any], raw["planted"]).items()
            ),
            named_contacts=len(hand_written),
        )
    except (KeyError, TypeError) as error:
        raise WorldError(f"The world is missing a required field: {error}") from error
    check_world(world)
    return world


def _contact(item: dict[str, str]) -> Contact:
    return Contact(
        item["id"],
        item["name"],
        item["email"],
        item["phone"],
        item["address"],
        item["relationship"],
        item["birthday"],
        item["notes"],
    )


def _message(item: dict[str, Any]) -> Message:
    return Message(
        item["id"],
        item["folder"],
        item["from_name"],
        item["from_email"],
        tuple(item["to"]),
        item["subject"],
        item["date"],
        item["body"],
        item["authored_by"],
    )


def _event(item: dict[str, Any]) -> Event:
    return Event(
        item["id"],
        item["title"],
        item["start"],
        item["end"],
        item["location"],
        tuple(item["attendees"]),
        item["notes"],
    )


def _bulk_contacts(count: int, domain: str) -> tuple[Contact, ...]:
    """A long tail of directory entries. Index-derived, so identical on every run."""
    people: list[Contact] = []
    for index in range(count):
        first = _FIRST[index % len(_FIRST)]
        last = _LAST[(index * 7 + index // len(_FIRST)) % len(_LAST)]
        people.append(
            Contact(
                id=f"c-bulk-{index + 1:03d}",
                name=f"{first} {last}",
                email=f"{first}.{last}.{index + 1:03d}@{domain}".lower(),
                phone=f"+1 555 {1000 + index:04d}",
                address="",
                relationship="directory",
                birthday="",
                notes="",
            )
        )
    return tuple(people)


def check_world(world: World) -> None:
    """Raise ``WorldError`` unless the world is internally consistent and safe to use."""
    problems: list[str] = []

    ids = [item.id for item in world.contacts]
    ids += [item.id for item in world.messages]
    ids += [item.id for item in world.events]
    duplicates = sorted({item for item in ids if ids.count(item) > 1})
    if duplicates:
        problems.append(f"duplicate ids: {', '.join(duplicates)}")

    for address in sorted(world.all_emails()):
        match = _EMAIL.match(address)
        if match is None:
            problems.append(f"not an address: {address!r}")
        elif match.group(1).rsplit(".", 1)[-1].lower() not in _RESERVED_TLDS:
            problems.append(f"{address} is not on a reserved test domain")

    for message in world.messages:
        if message.authored_by not in ("owner", "third_party"):
            problems.append(f"{message.id}: authored_by must be owner or third_party")

    for planted in world.planted:
        text = _locate(world, planted.where)
        if text is None:
            problems.append(f"planted {planted.name}: {planted.where!r} does not exist")
        elif planted.value not in text:
            problems.append(f"planted {planted.name}: value is not in {planted.where}")

    if problems:
        raise WorldError("; ".join(problems))


def _locate(world: World, where: str) -> str | None:
    """The text at ``kind:id.field`` (for example ``message:m-003.body``), or None."""
    match = re.fullmatch(r"(contact|message|event):([\w-]+)\.(\w+)", where)
    if match is None:
        return None
    kind, item_id, attribute = match.groups()
    pool: tuple[Any, ...] = {
        "contact": world.contacts,
        "message": world.messages,
        "event": world.events,
    }[kind]
    record = next((item for item in pool if item.id == item_id), None)
    if record is None or not hasattr(record, attribute):
        return None
    value = getattr(record, attribute)
    return value if isinstance(value, str) else None
