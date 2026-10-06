"""A sample MCP server for trying the adapter, with no authentication.

    python examples/sample_mcp_server.py [port]      # serves http://127.0.0.1:9101/mcp

It covers the parts of MCP the adapter has to carry through to Muse, so a connector built from
it shows whether each one survives the trip:

| Primitive | What it exercises |
| --- | --- |
| tool ``find_issue`` | a read-only tool with structured output |
| tool ``create_issue`` | a write |
| tool ``search_issues`` | optional parameters, an enum and an array |
| tool ``assign_issue`` | a nested object (a ``$defs`` reference in the schema) |
| tool ``break_things`` | a tool that reports an error |
| tool ``reindex`` | progress notifications |
| tool ``schedule_review`` | asks the user a question mid-call (elicitation) |
| tool ``export_all`` | a result larger than the adapter's size limit |
| resource ``workspace_guide`` | a fixed resource |
| template ``issue_document`` | a parameterised resource |
| prompt ``triage_issue`` | a prompt with arguments |
"""

import sys
from typing import Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel

ISSUES: dict[str, str] = {"ISSUE-1": "Login fails on Safari", "ISSUE-2": "Export is slow"}


class Person(BaseModel):
    name: str
    email: str


class Slot(BaseModel):
    time: str


def build_server(port: int = 9101) -> FastMCP:
    server = FastMCP(
        "Sample issue tracker",
        host="127.0.0.1",
        port=port,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*"],
        ),
    )
    read_only = ToolAnnotations(readOnlyHint=True)

    @server.tool(annotations=read_only)
    def find_issue(issue_id: str) -> dict[str, str]:
        """Look up an issue by its identifier."""
        return {"id": issue_id, "title": ISSUES.get(issue_id, "Unknown issue")}

    @server.tool()
    def create_issue(title: str) -> dict[str, str]:
        """Create a new issue."""
        identifier = f"ISSUE-{len(ISSUES) + 1}"
        ISSUES[identifier] = title
        return {"id": identifier, "title": title}

    @server.tool(annotations=read_only)
    def search_issues(
        query: str,
        status: Literal["open", "closed"] = "open",
        labels: list[str] | None = None,
        limit: int = 10,
    ) -> dict[str, object]:
        """Search issues by text, with optional status, labels and a result limit."""
        found = [
            {"id": key, "title": title}
            for key, title in ISSUES.items()
            if query.lower() in title.lower()
        ][:limit]
        return {"status": status, "labels": labels or [], "results": found}

    @server.tool()
    def assign_issue(issue_id: str, assignee: Person, watchers: list[Person] | None = None) -> str:
        """Assign an issue to a person, optionally adding watchers."""
        return f"{issue_id} assigned to {assignee.name} with {len(watchers or [])} watchers"

    @server.tool()
    def break_things() -> str:
        """Always fails, to see how a tool error is reported."""
        raise ValueError("The database is on fire")

    @server.tool(annotations=read_only)
    async def reindex(steps: int, ctx: Context) -> str:  # type: ignore[type-arg]
        """Rebuild the search index, reporting progress as it goes."""
        for step in range(steps):
            await ctx.report_progress(step + 1, steps, f"Indexed batch {step + 1}")
        return f"Reindexed {steps} batches"

    @server.tool()
    async def schedule_review(issue_id: str, ctx: Context) -> str:  # type: ignore[type-arg]
        """Schedule a review, asking the user which time works."""
        answer = await ctx.elicit(f"When should {issue_id} be reviewed?", Slot)
        if answer.action != "accept":
            return "No time chosen"
        return f"Review of {issue_id} scheduled for {answer.data.time}"

    @server.tool(annotations=read_only)
    def export_all() -> str:
        """Return a very large export."""
        return "x" * 2_000_000

    @server.resource("mcp://workspace/guide", name="workspace_guide", mime_type="text/plain")
    def workspace_guide() -> str:
        """How this workspace works."""
        return "Issues are triaged every Monday."

    @server.resource("mcp://issues/{issue_id}", name="issue_document", mime_type="text/plain")
    def issue_document(issue_id: str) -> str:
        """The full text of one issue."""
        return f"{issue_id}: {ISSUES.get(issue_id, 'Unknown issue')}"

    @server.prompt(name="triage_issue")
    def triage_issue(issue_id: str, focus: str = "impact") -> str:
        """A prompt that asks for a triage of one issue."""
        return f"Triage {issue_id} ({ISSUES.get(issue_id, 'unknown')}), focusing on {focus}."

    return server


if __name__ == "__main__":
    build_server(int(sys.argv[1]) if len(sys.argv) > 1 else 9101).run(transport="streamable-http")
