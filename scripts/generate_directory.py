#!/usr/bin/env python3
"""Regenerate the setup connector's list of services from the MCP Compat catalog.

    python3 scripts/generate_directory.py [PATH_TO_MCP_COMPAT]

Reads catalog/data/catalog.json and writes src/muse_mcp_adapter/chat_setup/directory_data.py.
Only a vendor's own hosted server with a streamable-HTTP address is listed. How the adapter signs
in follows the catalog's sign-in column:

    Open                                      -> none      (no sign-in)
    OAuth (DCR) / OAuth (DCR + CIMD)          -> oauth     (the owner signs in on the provider's page)
    OAuth (own client)                        -> oauth_app (the owner registers an app first)

A server that signs in only with a client ID metadata document, a local-only server and a
community-run one are skipped, and the skipped ones are printed so nobody wonders where they went.
Review the printed list before committing: a listed address is one the review page does not warn
about.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

OUTPUT = Path(__file__).resolve().parents[1] / "src/muse_mcp_adapter/chat_setup/directory_data.py"
DEFAULT_CATALOG = Path.home() / "Projects/mcp-compat"
PLANS = {
    "Open": "none",
    "OAuth (DCR)": "oauth",
    "OAuth (DCR + CIMD)": "oauth",
    "OAuth (own client)": "oauth_app",
}
HEADER = '''"""The services the setup connector can look up by name. GENERATED: do not edit by hand.

Made by scripts/generate_directory.py from the MCP Compat catalog (catalog tool {tool}, {count}
services). Each row is (id, name, address, how the adapter signs in, what it is for, topics, the
vendor's own page about its server). The hand-written entries in directory.py take precedence.
"""
# ruff: noqa: E501

from __future__ import annotations

ENTRIES: tuple[tuple[str, str, str, str, str, tuple[str, ...], str], ...] = (
'''


def entries_from(catalog: dict[str, Any]) -> tuple[list[tuple[Any, ...]], list[str]]:
    """The rows to list, and a line for each server that was left out."""
    rows: list[tuple[Any, ...]] = []
    skipped: list[str] = []
    for server in sorted(catalog["servers"], key=lambda item: str(item["slug"])):
        slug = str(server["slug"])
        remote = [
            endpoint
            for endpoint in server.get("endpoints", [])
            if endpoint.get("kind") == "remote"
            and endpoint.get("transport") == "streamable-http"
            and str(endpoint.get("url") or "").startswith("https://")
        ]
        if not remote:
            skipped.append(f"{slug}: no hosted streamable-HTTP address")
            continue
        if server.get("operator") != "first-party":
            skipped.append(f"{slug}: not run by the vendor ({server.get('operator')})")
            continue
        plan = PLANS.get(str(server.get("sign_in")))
        if plan is None:
            skipped.append(f"{slug}: sign-in '{server.get('sign_in')}' is not supported yet")
            continue
        primary = next((item for item in remote if item.get("primary")), remote[0])
        topics = list(dict.fromkeys([*server.get("categories", []), *server.get("tags", [])]))
        rows.append(
            (
                slug,
                str(server.get("vendor") or server["name"]),
                str(primary["url"]),
                plan,
                " ".join(str(server.get("summary", "")).split()),
                tuple(str(topic) for topic in topics),
                str(server.get("docs") or server.get("homepage") or ""),
            )
        )
    return rows, skipped


def render(rows: list[tuple[Any, ...]], tool: str) -> str:
    lines = [HEADER.format(tool=tool, count=len(rows))]
    for slug, name, address, plan, summary, topics, docs in rows:
        lines.append("    (\n")
        for value in (slug, name, address, plan, summary):
            lines.append(f"        {json.dumps(value, ensure_ascii=False)},\n")
        shown = ", ".join(json.dumps(topic) for topic in topics)
        lines.append(f"        ({shown}{',' if len(topics) == 1 else ''}),\n")
        lines.append(f"        {json.dumps(docs, ensure_ascii=False)},\n")
        lines.append("    ),\n")
    lines.append(")\n")
    return "".join(lines)


def main(argv: list[str]) -> int:
    root = Path(argv[1]).expanduser() if len(argv) > 1 else DEFAULT_CATALOG
    catalog = json.loads((root / "catalog/data/catalog.json").read_text())
    rows, skipped = entries_from(catalog)
    OUTPUT.write_text(render(rows, str(catalog.get("tool_version", "unknown"))))
    print(f"wrote {len(rows)} services to {OUTPUT.relative_to(OUTPUT.parents[3])}")
    for row in rows:
        print(f"  {row[0]:<16} {row[3]:<10} {row[2]}")
    for line in skipped:
        print(f"  skipped {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
