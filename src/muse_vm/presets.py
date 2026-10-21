"""Well-known MCP servers, so `muse-vm add-server github` needs nothing else.

Each preset names the server's public address and how the adapter signs in to it. Everything can
still be overridden with flags; a server that is not listed works exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    endpoint: str
    credential: str  # none | env_token | oauth
    summary: str
    secret_name: str | None = None
    token_help: str | None = None  # where to create the token, shown before asking for it
    token_shape: str | None = None  # a pattern a real token matches, to catch a wrong paste
    token_kind: str | None = None  # what to call it when it does not match ("a GitHub token")


PRESETS: dict[str, Preset] = {
    preset.id: preset
    for preset in (
        Preset(
            "learn",
            "Microsoft Learn",
            "https://learn.microsoft.com/api/mcp",
            "none",
            "search and read official Microsoft and Azure documentation (no sign-in)",
        ),
        Preset(
            "github",
            "GitHub",
            "https://api.githubcopilot.com/mcp/",
            "env_token",
            "repositories, issues and pull requests",
            secret_name="GITHUB_TOKEN",
            token_shape=r"^((github_pat_|gh[pousr]_)[A-Za-z0-9_]{20,}|[0-9a-f]{40})$",
            token_kind="a GitHub token (they start with github_pat_ or ghp_)",
            token_help=(
                "Create a fine-grained token at "
                "https://github.com/settings/personal-access-tokens/new limited to the "
                "repositories Muse should use (read access is enough to start; add Issues and "
                "Pull requests write later if you want Muse to change things)."
            ),
        ),
        Preset(
            "linear",
            "Linear",
            "https://mcp.linear.app/mcp",
            "oauth",
            "issues and projects (you approve access in your browser)",
        ),
        Preset(
            "atlassian",
            "Atlassian",
            "https://mcp.atlassian.com/v2/mcp",
            "oauth",
            "Jira and Confluence (you approve access in your browser)",
        ),
    )
}


def find(server: str) -> Preset | None:
    return PRESETS.get(server.lower())


def describe() -> list[str]:
    width = max(len(item) for item in PRESETS)
    return [f"{p.id.ljust(width)}  {p.name}: {p.summary}" for p in PRESETS.values()]
