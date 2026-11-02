"""The vetted list of servers the setup connector can look up by name.

An entry gives the official address and how the adapter signs in, so a proposal for a listed
server needs no address from the agent. A server that is not listed can still be proposed by its
address; the owner is warned and nothing contacts it before the owner acts.

The few entries written here carry what only a person can know (for example which kind of token to
create) and take precedence. The rest come from ``directory_data``, generated from the MCP Compat
catalog by ``scripts/generate_directory.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .directory_data import ENTRIES as GENERATED

CREDENTIALS = ("none", "env_token", "oauth", "oauth_app")


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    id: str
    name: str
    endpoint: str
    credential: str  # none | env_token | oauth | oauth_app
    summary: str
    secret_label: str | None = None
    token_help: str | None = None
    topics: tuple[str, ...] = ()
    docs: str | None = None  # the vendor's own page about its MCP server

    def sign_in_plan(self) -> str:
        if self.credential == "none":
            return "No sign-in is needed."
        if self.credential == "oauth":
            return "The owner signs in once on the provider's own page; no secret is typed."
        if self.credential == "oauth_app":
            return (
                "The provider needs an app the owner registers first. The owner types its client "
                "ID and secret on the review page (never into the chat), then signs in on the "
                "provider's own page."
            )
        return "The owner pastes a token on the review page (never into the chat)."


PRESETS: tuple[DirectoryEntry, ...] = (
    DirectoryEntry(
        "learn",
        "Microsoft Learn",
        "https://learn.microsoft.com/api/mcp",
        "none",
        "search and read official Microsoft and Azure documentation (no sign-in)",
    ),
    DirectoryEntry(
        "github",
        "GitHub",
        "https://api.githubcopilot.com/mcp/",
        "env_token",
        "repositories, issues and pull requests",
        secret_label="GitHub token",
        token_help=(
            "Create a fine-grained token at "
            "https://github.com/settings/personal-access-tokens/new limited to the "
            "repositories Muse should use (read access is enough to start; add Issues and "
            "Pull requests write later if you want Muse to change things)."
        ),
    ),
    DirectoryEntry(
        "linear",
        "Linear",
        "https://mcp.linear.app/mcp",
        "oauth",
        "issues and projects (you approve access in your browser)",
    ),
    DirectoryEntry(
        "atlassian",
        "Atlassian",
        "https://mcp.atlassian.com/v2/mcp",
        "oauth",
        "Jira and Confluence (you approve access in your browser)",
    ),
)

_WORDS = re.compile(r"[a-z0-9]+")


def _same_address(first: str, second: str) -> bool:
    return first.rstrip("/").lower() == second.rstrip("/").lower()


def _build() -> tuple[tuple[DirectoryEntry, ...], dict[str, str]]:
    entries = list(PRESETS)
    aliases: dict[str, str] = {}
    for identifier, name, endpoint, credential, summary, topics, docs in GENERATED:
        entry = DirectoryEntry(
            identifier, name, endpoint, credential, summary, topics=topics, docs=docs or None
        )
        shadowing = next(
            (
                preset
                for preset in PRESETS
                if preset.id == identifier or _same_address(preset.endpoint, endpoint)
            ),
            None,
        )
        if shadowing is None:
            entries.append(entry)
        elif shadowing.id != identifier:  # the same service under another name: accept both
            aliases[identifier] = shadowing.id
    return tuple(entries), aliases


ENTRIES, ALIASES = _build()


def get(server_id: str) -> DirectoryEntry | None:
    wanted = ALIASES.get(server_id, server_id)
    return next((entry for entry in ENTRIES if entry.id == wanted), None)


def _words(text: str) -> set[str]:
    """Lower-case words with a plural or -ing ending removed, so 'monitoring' meets 'monitors'."""
    found: set[str] = set()
    for word in _WORDS.findall(text.lower()):
        if len(word) > 5 and word.endswith("ing"):
            word = word[:-3]
        elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        found.add(word)
    return found


def search(query: str, *, limit: int = 5) -> list[DirectoryEntry]:
    """Entries whose id, name, summary or topics share a word with the query, best match first."""
    wanted = _words(query)
    if not wanted:
        return []
    ids = set(_WORDS.findall(query.lower()))
    scored: list[tuple[int, DirectoryEntry]] = []
    for entry in ENTRIES:
        text = f"{entry.id} {entry.name} {entry.summary} {' '.join(entry.topics)}"
        score = len(wanted & _words(text)) + (3 if entry.id in ids else 0)
        if score:
            scored.append((score, entry))
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    return [entry for _, entry in scored[:limit]]
