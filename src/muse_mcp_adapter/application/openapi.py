from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import cast

from ..catalog.canonical import thaw_json
from ..domain.models import PublishedCatalog, PublishedPrimitive

_TEMPLATE_FIELD = re.compile(r"\{([A-Za-z][A-Za-z0-9_]*)\}")


def compile_openapi(catalog: PublishedCatalog) -> Mapping[str, object]:
    """Compile an active immutable catalog into a Muse-facing OpenAPI document."""
    server_id = str(catalog.server.get("id", ""))
    paths: dict[str, object] = {}
    for primitive in catalog.primitives:
        paths[f"/v1/operations/{primitive.public_operation_id}"] = {
            "post": {
                "operationId": primitive.public_operation_id,
                "summary": _summary(primitive),
                "security": [{"bearerAuth": []}],
                "x-muse-classification": primitive.classification,
                "x-mcp-primitive-type": primitive.primitive_type,
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "serverId": {"type": "string", "const": server_id},
                                    "connectionId": {"type": "string", "minLength": 1},
                                    "requestId": {"type": "string", "minLength": 1},
                                    "arguments": _argument_schema(primitive),
                                    "idempotencyKey": {"type": "string", "minLength": 1},
                                    "approval": {"type": "object"},
                                },
                                "required": ["serverId", "connectionId", "arguments"],
                                "additionalProperties": False,
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "Normalized MCP result"},
                    "400": {"description": "Invalid request"},
                    "401": {"description": "Authentication failed"},
                    "403": {"description": "Authorization failed"},
                    "409": {"description": "Approval, lifecycle, or idempotency conflict"},
                    "502": {"description": "Downstream MCP failure"},
                },
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": f"Muse MCP Adapter — {server_id}",
            "version": catalog.catalog_version,
        },
        "paths": paths,
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"}
            }
        },
    }


def _summary(primitive: PublishedPrimitive) -> str:
    value = primitive.definition.get("description") or primitive.definition.get("title")
    return value if isinstance(value, str) else primitive.mcp_name


def _argument_schema(primitive: PublishedPrimitive) -> object:
    if primitive.primitive_type == "tool":
        return thaw_json(primitive.definition.get("inputSchema", {"type": "object"}))
    if primitive.primitive_type == "resource":
        return {"type": "object", "additionalProperties": False}
    if primitive.primitive_type == "resource_template":
        template = primitive.definition.get("uriTemplate")
        fields = _TEMPLATE_FIELD.findall(template) if isinstance(template, str) else []
        return {
            "type": "object",
            "properties": {name: {"type": "string"} for name in fields},
            "required": fields,
            "additionalProperties": False,
        }
    raw_arguments = primitive.definition.get("arguments", ())
    arguments = (
        cast(Sequence[object], raw_arguments)
        if isinstance(raw_arguments, Sequence)
        and not isinstance(raw_arguments, (str, bytes, bytearray))
        else ()
    )
    properties: dict[str, object] = {}
    required: list[str] = []
    for raw in arguments:
        if not isinstance(raw, Mapping):
            continue
        item = cast(Mapping[str, object], raw)
        name = item.get("name")
        if not isinstance(name, str):
            continue
        properties[name] = {"type": "string"}
        if item.get("required") is True:
            required.append(name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
