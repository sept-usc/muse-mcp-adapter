"""Drafting an exposure policy from discovery."""

from muse_mcp_adapter.application.policy_template import draft_policies
from muse_mcp_adapter.control_plane.compile_catalog import compile_catalog
from muse_mcp_adapter.discovery.client import ManifestDiscoveryClient
from muse_mcp_adapter.domain.models import DiscoverySnapshot, ServerRegistration

_NOW = "2026-10-05T10:00:00Z"
_SERVER = ServerRegistration(
    "s-one", "S", "https://mcp.example.test/mcp", "x", None, True, _NOW, _NOW
)


def _snapshot(primitives: list[dict[str, object]]) -> DiscoverySnapshot:
    manifest = {
        "protocolVersion": "2025-11-25",
        "serverInfo": {"name": "s"},
        "capabilities": {},
        "primitives": primitives,
    }
    return ManifestDiscoveryClient(manifest, discovered_at=_NOW).discover(_SERVER)


_PRIMITIVES: list[dict[str, object]] = [
    {
        "type": "tool",
        "name": "Find-Issue",
        "inputSchema": {"type": "object"},
        "annotations": {"readOnlyHint": True},
    },
    {"type": "tool", "name": "create issue", "inputSchema": {"type": "object"}},
    {"type": "tool", "name": "x", "inputSchema": {"type": "object"}},
    {"type": "tool", "name": "get_operation_status", "inputSchema": {"type": "object"}},
    {
        "type": "resource",
        "name": "docs",
        "uri": "https://docs.example.test/start",
        "mimeType": "text/markdown",
    },
    {"type": "resource_template", "name": "doc", "uriTemplate": "mcp://docs/{id}"},
    {"type": "prompt", "name": "triage", "arguments": []},
]


def test_nothing_is_exposed_unless_asked_and_writes_never_are() -> None:
    cautious = draft_policies(_snapshot(_PRIMITIVES))
    assert not any(entry["allowed"] for entry in cautious)

    reads = {
        e["primitiveKey"]: e for e in draft_policies(_snapshot(_PRIMITIVES), expose_reads=True)
    }
    assert reads["tool:Find-Issue"]["allowed"] is True
    assert reads["resource:docs"]["allowed"] is True
    assert reads["prompt:triage"]["allowed"] is True
    assert reads["tool:create issue"]["allowed"] is False
    assert reads["tool:create issue"]["classification"] == "write"


def test_operation_ids_are_valid_unique_and_avoid_reserved_names() -> None:
    entries = draft_policies(_snapshot(_PRIMITIVES))
    ids = [str(entry["publicOperationId"]) for entry in entries]
    assert len(ids) == len(set(ids))
    assert ids[:2] == ["find_issue", "create_issue"]
    assert "get_operation_status" not in ids
    import re

    assert all(re.fullmatch(r"[a-z][a-z0-9_]{2,63}", value) for value in ids)


def test_resource_policies_carry_scheme_host_and_mime_limits() -> None:
    entries = {e["primitiveKey"]: e for e in draft_policies(_snapshot(_PRIMITIVES))}
    docs: dict[str, object] = entries["resource:docs"]["resource"]  # type: ignore[assignment]
    assert docs["allowedUriSchemes"] == ["https"]
    assert docs["allowedUriHosts"] == ["docs.example.test"]
    assert docs["allowedMimeTypes"] == ["text/markdown"]
    template: dict[str, object] = entries["resource_template:doc"]["resource"]  # type: ignore[assignment]
    assert template["allowedUriSchemes"] == ["mcp"] and "allowedUriHosts" not in template


def test_a_reviewed_draft_publishes_without_edits() -> None:
    snapshot = _snapshot(_PRIMITIVES[:2] + _PRIMITIVES[4:])
    drafts = draft_policies(snapshot, expose_reads=True)
    for entry in drafts:
        entry["allowed"] = True  # the owner reviewed everything and switched it all on
    catalog = compile_catalog(
        catalog_version="1.0.0",
        server={"id": "s-one", "endpoint": "https://mcp.example.test/mcp"},
        protocol_version="2025-11-25",
        discovered_at=_NOW,
        published_at=_NOW,
        primitives=snapshot.primitives,
        policies=drafts,
    )
    assert len(catalog.primitives) == len(drafts)
