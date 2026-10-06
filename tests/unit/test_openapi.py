"""The OpenAPI document Muse reads when the owner asks it to build the connector."""

from collections.abc import Mapping
from typing import Any

import pytest
from openapi_spec_validator import validate

from muse_mcp_adapter.application.openapi import (
    GET_OPERATION_ID,
    RESUME_OPERATION_ID,
    compile_openapi,
)
from muse_mcp_adapter.control_plane.compile_catalog import compile_catalog
from muse_mcp_adapter.domain.errors import CatalogValidationError

_NOW = "2026-10-05T10:00:00Z"
_TEAM_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "team": {"$ref": "#/$defs/Team"},
        "peers": {"type": "array", "items": {"$ref": "#/$defs/Team"}},
    },
    "required": ["title"],
    "$defs": {"Team": {"type": "object", "properties": {"name": {"type": "string"}}}},
}


def _catalog(primitives: list[Mapping[str, object]], policies: list[Mapping[str, object]]):  # type: ignore[no-untyped-def]
    return compile_catalog(
        catalog_version="2.0.0",
        server={"id": "project-system", "endpoint": "https://mcp.example.test/mcp"},
        protocol_version="2025-11-25",
        discovered_at=_NOW,
        published_at=_NOW,
        primitives=primitives,
        policies=policies,
    )


def _tool(name: str = "create_issue", **extra: object) -> dict[str, object]:
    return {"type": "tool", "name": name, "inputSchema": _TEAM_SCHEMA, **extra}


def _tool_policy(name: str = "create_issue", classification: str = "write") -> dict[str, object]:
    return {
        "primitiveKey": f"tool:{name}",
        "allowed": True,
        "publicOperationId": name,
        "classification": classification,
        "maxResultBytes": 1000,
    }


def test_schema_definitions_are_hoisted_so_references_resolve() -> None:
    catalog = _catalog([_tool()], [_tool_policy()])
    document = compile_openapi(catalog, public_url="https://a.example.com/", slug="abc")
    validate(dict(document))

    paths: Any = document["paths"]
    schema = paths["/operations/create_issue"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert "$defs" not in schema and "$schema" not in schema
    assert schema["properties"]["team"] == {"$ref": "#/components/schemas/create_issue__Team"}
    assert schema["properties"]["peers"]["items"] == {
        "$ref": "#/components/schemas/create_issue__Team"
    }
    assert "create_issue__Team" in document["components"]["schemas"]  # type: ignore[index,operator]
    assert document["servers"] == [{"url": "https://a.example.com/c/abc"}]


def test_each_primitive_kind_gets_a_flat_typed_body() -> None:
    primitives: list[Mapping[str, object]] = [
        _tool("find_issue", annotations={"readOnlyHint": True}),
        {"type": "resource", "name": "guide", "uri": "mcp://workspace/guide"},
        {"type": "resource_template", "name": "issue_doc", "uriTemplate": "mcp://issues/{issueId}"},
        {
            "type": "prompt",
            "name": "triage",
            "arguments": [
                {"name": "issueId", "required": True, "description": "Which issue"},
                {"name": "focus"},
            ],
        },
    ]
    resource = {"allowedMimeTypes": ["text/plain"], "maxBytes": 100}
    catalog = _catalog(
        primitives,
        [
            _tool_policy("find_issue", "read") | {"readOnlyReviewed": True},
            {
                "primitiveKey": "resource:guide",
                "allowed": True,
                "publicOperationId": "read_guide",
                "classification": "read",
                "resource": resource,
            },
            {
                "primitiveKey": "resource_template:issue_doc",
                "allowed": True,
                "publicOperationId": "read_issue",
                "classification": "read",
                "resource": resource | {"argumentPatterns": {"issueId": "[A-Z]+-[0-9]+"}},
            },
            {
                "primitiveKey": "prompt:triage",
                "allowed": True,
                "publicOperationId": "triage_prompt",
                "classification": "read",
                "prompt": {
                    "allowedRoles": ["user"],
                    "allowedEmbeddedMimeTypes": [],
                    "maxBytes": 100,
                },
            },
        ],
    )
    document = compile_openapi(catalog, public_url="https://a.example.com", slug="abc")
    validate(dict(document))
    paths: Any = document["paths"]

    assert "requestBody" not in paths["/operations/read_guide"]["post"]
    template = paths["/operations/read_issue"]["post"]["requestBody"]["content"]["application/json"]
    assert template["schema"]["required"] == ["issueId"]
    assert template["schema"]["properties"]["issueId"]["pattern"] == "^(?:[A-Z]+-[0-9]+)$"
    prompt = paths["/operations/triage_prompt"]["post"]["requestBody"]["content"][
        "application/json"
    ]
    assert prompt["schema"]["required"] == ["issueId"]
    assert prompt["schema"]["properties"]["issueId"]["description"] == "Which issue"
    assert prompt["schema"]["additionalProperties"] is False


def test_classification_is_stated_for_the_model_and_gates_confirmation() -> None:
    catalog = _catalog(
        [_tool("create_issue", description="Create an issue"), _tool("wipe", description="Wipe")],
        [_tool_policy("create_issue", "write"), _tool_policy("wipe", "sensitive_write")],
    )
    paths: Any = compile_openapi(catalog, public_url="https://a.example.com", slug="s")["paths"]
    write = paths["/operations/create_issue"]["post"]
    sensitive = paths["/operations/wipe"]["post"]

    assert write["description"].startswith("Create an issue")
    assert "confirm" in write["description"].lower()
    assert "explicitly confirm" in sensitive["description"]
    assert write["x-requires-confirmation"] and sensitive["x-requires-confirmation"]
    assert sensitive["x-muse-classification"] == "sensitive_write"


def test_status_operation_is_always_present_and_resume_only_with_tools() -> None:
    resource_only = _catalog(
        [{"type": "resource", "name": "guide", "uri": "mcp://workspace/guide"}],
        [
            {
                "primitiveKey": "resource:guide",
                "allowed": True,
                "publicOperationId": "read_guide",
                "classification": "read",
                "resource": {"allowedMimeTypes": ["text/plain"], "maxBytes": 100},
            }
        ],
    )
    paths: Any = compile_openapi(resource_only, public_url="https://a.example.com", slug="s")[
        "paths"
    ]
    assert paths["/executions/{operationId}"]["get"]["operationId"] == GET_OPERATION_ID
    assert "/executions/{operationId}/resume" not in paths

    with_tool = compile_openapi(
        _catalog([_tool()], [_tool_policy()]), public_url="https://a.example.com", slug="s"
    )
    resume = with_tool["paths"]["/executions/{operationId}/resume"]["post"]  # type: ignore[index]
    assert resume["operationId"] == RESUME_OPERATION_ID


@pytest.mark.parametrize("reserved", [GET_OPERATION_ID, RESUME_OPERATION_ID])
def test_reserved_operation_names_cannot_be_published(reserved: str) -> None:
    with pytest.raises(CatalogValidationError):
        _catalog([_tool("a_tool")], [_tool_policy("a_tool") | {"publicOperationId": reserved}])


def test_every_operation_id_is_unique_and_errors_share_one_schema() -> None:
    catalog = _catalog(
        [_tool("alpha_tool"), _tool("beta_tool")],
        [_tool_policy("alpha_tool"), _tool_policy("beta_tool")],
    )
    document: Any = compile_openapi(catalog, public_url="https://a.example.com", slug="s")
    ids = [
        operation["operationId"]
        for item in document["paths"].values()
        for operation in item.values()
    ]
    assert len(ids) == len(set(ids)) == 4
    error = document["paths"]["/operations/alpha_tool"]["post"]["responses"]["409"]
    assert error["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Problem"
    }
