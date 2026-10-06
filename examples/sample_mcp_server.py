"""A tiny MCP server for trying the adapter locally (no authentication).

    python examples/sample_mcp_server.py            # serves http://127.0.0.1:9101/mcp

It exposes one read-only tool, one write tool and one resource.
"""

import sys

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

port = int(sys.argv[1]) if len(sys.argv) > 1 else 9101
server = FastMCP("Sample issue tracker", host="127.0.0.1", port=port)
issues: dict[str, str] = {"ISSUE-1": "Login fails on Safari"}


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def find_issue(issue_id: str) -> dict[str, str]:
    """Look up an issue by its identifier."""
    return {"id": issue_id, "title": issues.get(issue_id, "Unknown issue")}


@server.tool()
def create_issue(title: str) -> dict[str, str]:
    """Create a new issue."""
    identifier = f"ISSUE-{len(issues) + 1}"
    issues[identifier] = title
    return {"id": identifier, "title": title}


@server.resource("mcp://workspace/guide", name="workspace_guide", mime_type="text/plain")
def workspace_guide() -> str:
    """How this workspace works."""
    return "Issues are triaged every Monday."


if __name__ == "__main__":
    server.run(transport="streamable-http")
