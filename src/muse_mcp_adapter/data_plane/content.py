"""One content normalizer shared by tools, resources and prompts.

Every MCP content type the protocol defines is carried through unchanged (text, images, audio,
resource links, embedded text and binary resources) as long as its MIME type and size are within
what the owner's policy allows. Nothing is silently dropped or rewritten.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Collection, Mapping
from typing import cast

from ..catalog.canonical import freeze_json
from ..domain.errors import DownstreamContentError
from ..domain.models import McpContentBlock, McpResourceContent, NormalizedContent

# What a tool may return unless the owner narrows it. MCP does not declare result types.
DEFAULT_TOOL_RESULT_TYPES = ("text/*", "application/json", "image/*", "audio/*")


def mime_allowed(mime_type: str, allowed: Collection[str]) -> bool:
    """Whether ``mime_type`` matches an allowed entry; ``type/*`` and ``*/*`` are wildcards."""
    base = mime_type.split(";", 1)[0].strip().lower()
    major = base.split("/", 1)[0]
    for entry in allowed:
        pattern = entry.strip().lower()
        if pattern in (base, "*/*") or (pattern.endswith("/*") and pattern[:-1] == f"{major}/"):
            return True
    return False


def narrow_types(declared: Collection[str], narrowed: Collection[str]) -> frozenset[str]:
    """The owner's narrower list, restricted to what the declaration already covers."""
    return frozenset(item for item in narrowed if mime_allowed(item.replace("/*", "/x"), declared))


def decoded_size(data: str) -> int:
    """The size in bytes of base64 data, rejecting anything that is not valid base64."""
    try:
        return len(base64.b64decode(data, validate=True))
    except (binascii.Error, ValueError) as error:
        raise DownstreamContentError("Binary content is not valid base64") from error


def normalize_block(
    block: McpContentBlock, allowed: Collection[str], *, noun: str
) -> tuple[NormalizedContent, int]:
    """Convert one MCP content block; returns it with the number of bytes it carries."""
    annotations = cast(Mapping[str, object], freeze_json(dict(block.annotations or {})))
    kind = block.block_type
    if kind == "text" and block.text is not None:
        if not mime_allowed("text/plain", allowed):
            raise DownstreamContentError(f"{noun} text content is not allowed")
        return (
            NormalizedContent("text", block.text, "text/plain", None, annotations),
            len(block.text.encode()),
        )
    if kind in ("image", "audio") and block.data is not None and block.mime_type:
        if not mime_allowed(block.mime_type, allowed):
            raise DownstreamContentError(f"{noun} {kind} type {block.mime_type} is not allowed")
        size = decoded_size(block.data)
        return (
            NormalizedContent(kind, "", block.mime_type, None, annotations, data=block.data),
            size,
        )
    if kind == "resource_link" and block.uri:
        return (
            NormalizedContent(
                "resource_link", "", block.mime_type or "", block.uri, annotations, name=block.name
            ),
            len(block.uri.encode()),
        )
    if kind == "resource" and block.resource is not None:
        return normalize_resource(block.resource, allowed, noun=noun, annotations=annotations)
    raise DownstreamContentError(f"Unsupported {noun.lower()} content block {kind}")


def normalize_resource(
    content: McpResourceContent,
    allowed: Collection[str],
    *,
    noun: str,
    annotations: Mapping[str, object] | None = None,
) -> tuple[NormalizedContent, int]:
    if not mime_allowed(content.mime_type, allowed):
        raise DownstreamContentError(f"{noun} MIME type {content.mime_type} is not allowed")
    details = annotations
    if details is None:
        details = cast(Mapping[str, object], freeze_json(dict(content.annotations or {})))
    if content.blob is not None:
        data = content.blob.decode("ascii", errors="replace")
        size = decoded_size(data)
        return (
            NormalizedContent("resource", "", content.mime_type, content.uri, details, data=data),
            size,
        )
    if content.text is None:
        raise DownstreamContentError(f"{noun} resource content is empty")
    return (
        NormalizedContent("resource", content.text, content.mime_type, content.uri, details),
        len(content.text.encode()),
    )
