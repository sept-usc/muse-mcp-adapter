# pyright: basic
"""Compare what an MCP server returns directly with what the connector returns for the same call."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from mcp import types


class Verdict(StrEnum):
    SAME = "SAME"  # identical content and meaning
    TRANSLATED = "TRANSLATED"  # same information through the REST equivalent of the MCP feature
    POLICY = "POLICY"  # differs because the owner's policy narrows what is exposed
    UNSUPPORTED = "UNSUPPORTED"  # an MCP capability the connector does not carry
    NOT_APPLICABLE = "NOT_APPLICABLE"  # protocol-level behaviour with no REST counterpart


@dataclass
class Observation:
    scenario: str
    area: str
    verdict: Verdict
    direct: str
    connector: str
    note: str = ""
    standard: str | None = None  # the official conformance scenario this corresponds to


@dataclass
class ParityLog:
    observations: list[Observation] = field(default_factory=lambda: [])

    def add(self, observation: Observation) -> None:
        self.observations = [
            item for item in self.observations if item.scenario != observation.scenario
        ] + [observation]

    def counts(self) -> dict[str, int]:
        return {
            verdict.value: sum(1 for item in self.observations if item.verdict == verdict)
            for verdict in Verdict
        }

    def markdown(self) -> str:
        lines = [
            "# MCP parity: connector versus the MCP server directly",
            "",
            "| Verdict | Count |",
            "| --- | --- |",
            *[f"| {name} | {count} |" for name, count in self.counts().items()],
            "",
            "| Area | Scenario | Official scenario | Verdict | Direct | Connector | Note |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for item in sorted(self.observations, key=lambda o: (o.area, o.scenario)):
            row = [
                item.area,
                item.scenario,
                item.standard or "",
                item.verdict.value,
                item.direct,
                item.connector,
                item.note,
            ]
            lines.append("| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |")
        return "\n".join(lines) + "\n"

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.markdown())


# --- normalization -------------------------------------------------------------------------


def _blob(data: str) -> dict[str, Any]:
    raw = base64.b64decode(data)
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()[:16]}


def direct_blocks(blocks: Sequence[Any]) -> list[dict[str, Any]]:
    """MCP content blocks in a comparable form."""
    result: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, types.TextContent):
            result.append({"type": "text", "text": block.text})
        elif isinstance(block, types.ImageContent):
            result.append({"type": "image", "mime": block.mimeType, **_blob(block.data)})
        elif isinstance(block, types.AudioContent):
            result.append({"type": "audio", "mime": block.mimeType, **_blob(block.data)})
        elif isinstance(block, types.ResourceLink):
            result.append(
                {
                    "type": "resource_link",
                    "uri": str(block.uri),
                    "name": block.name,
                    "mime": block.mimeType,
                }
            )
        elif isinstance(block, types.EmbeddedResource):
            result.append({"type": "resource", **direct_resource(block.resource)})
    return result


def direct_resource(
    content: types.TextResourceContents | types.BlobResourceContents,
) -> dict[str, Any]:
    if isinstance(content, types.TextResourceContents):
        return {"uri": str(content.uri), "mime": content.mimeType, "text": content.text}
    return {"uri": str(content.uri), "mime": content.mimeType, **_blob(content.blob)}


def connector_blocks(contents: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The connector's ``contents`` in the same comparable form."""
    result: list[dict[str, Any]] = []
    for item in contents:
        kind = item["kind"]
        if kind == "text":
            result.append({"type": "text", "text": item["text"]})
        elif kind in ("image", "audio"):
            result.append({"type": kind, "mime": item["mimeType"], **_blob(item["data"])})
        elif kind == "resource_link":
            result.append(
                {
                    "type": "resource_link",
                    "uri": item["uri"],
                    "name": item.get("name"),
                    "mime": item.get("mimeType"),
                }
            )
        else:
            result.append({"type": "resource", **connector_resource(item)})
    return result


def connector_resource(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("data") is not None:
        return {"uri": item["uri"], "mime": item["mimeType"], **_blob(item["data"])}
    return {"uri": item["uri"], "mime": item["mimeType"], "text": item["text"]}


def expand_refs(schema: Any, definitions: dict[str, Any], prefix: str) -> Any:
    """Inline ``$ref`` targets so schemas can be compared regardless of where they are stored."""
    if isinstance(schema, dict):
        reference = schema.get("$ref")
        if isinstance(reference, str) and reference.startswith(prefix):
            return expand_refs(definitions[reference.removeprefix(prefix)], definitions, prefix)
        return {
            key: expand_refs(value, definitions, prefix)
            for key, value in schema.items()
            if key not in ("$defs", "definitions", "$schema")
        }
    if isinstance(schema, list):
        return [expand_refs(item, definitions, prefix) for item in schema]
    return schema
