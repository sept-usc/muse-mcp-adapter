"""The vetted list of servers the setup connector can look up by name.

An entry gives the official address and how the adapter signs in, so a proposal for a listed
server needs no address from the agent. A server that is not listed can still be proposed by its
address; the owner is warned and nothing contacts it before the owner acts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    id: str
    name: str
    endpoint: str
    credential: str  # none | env_token | oauth
    summary: str
    secret_label: str | None = None
    token_help: str | None = None

    def sign_in_plan(self) -> str:
        if self.credential == "none":
            return "No sign-in is needed."
        if self.credential == "oauth":
            return "The owner signs in once on the provider's own page; no secret is typed."
        return "The owner pastes a token on the review page (never into the chat)."


ENTRIES: tuple[DirectoryEntry, ...] = (
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


def get(server_id: str) -> DirectoryEntry | None:
    return next((entry for entry in ENTRIES if entry.id == server_id), None)


def search(query: str, *, limit: int = 5) -> list[DirectoryEntry]:
    """Entries whose id, name or summary share a word with the query, best match first."""
    wanted = set(_WORDS.findall(query.lower()))
    if not wanted:
        return []
    scored: list[tuple[int, DirectoryEntry]] = []
    for entry in ENTRIES:
        haystack = set(_WORDS.findall(f"{entry.id} {entry.name} {entry.summary}".lower()))
        score = len(wanted & haystack) + (3 if entry.id in wanted else 0)
        if score:
            scored.append((score, entry))
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    return [entry for _, entry in scored[:limit]]
