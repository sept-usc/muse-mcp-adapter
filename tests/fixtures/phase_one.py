from typing import Any


def phase_one_fixture() -> dict[str, Any]:
    primitives = [
        {
            "type": "tool",
            "name": "find_issue",
            "description": "Find an issue by identifier",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
                "additionalProperties": False,
            },
            "outputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "title": {"type": "string"}},
                "required": ["id", "title"],
            },
            "annotations": {"readOnlyHint": True},
            "requiredScopes": ["issues:read"],
            "resultContentTypes": ["application/json", "text/plain"],
            "lifecycleModes": ["synchronous"],
        },
        {
            "type": "tool",
            "name": "create_issue",
            "description": "Create an issue",
            "inputSchema": {
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
                "additionalProperties": False,
            },
            "outputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
            "annotations": {"readOnlyHint": False, "idempotentHint": True},
            "requiredScopes": ["issues:write"],
            "resultContentTypes": ["application/json", "text/plain"],
            "lifecycleModes": ["synchronous"],
        },
        {
            "type": "resource",
            "name": "workspace_guide",
            "uri": "mcp://workspace/guide",
            "requiredScopes": ["workspace:read"],
        },
        {
            "type": "resource_template",
            "name": "issue_document",
            "uriTemplate": "mcp://issues/{issueId}/document",
            "requiredScopes": ["issues:read"],
        },
        {
            "type": "prompt",
            "name": "triage_issue",
            "arguments": [
                {"name": "issueId", "required": True},
                {"name": "focus", "required": False},
            ],
            "requiredScopes": ["issues:read"],
        },
    ]

    policies = [
        {
            "primitiveKey": "tool:find_issue",
            "allowed": True,
            "publicOperationId": "find_issue",
            "classification": "read",
            "requiredScopes": ["issues:read"],
            "timeoutSeconds": 15,
            "maxResultBytes": 262144,
        },
        {
            "primitiveKey": "tool:create_issue",
            "allowed": True,
            "publicOperationId": "create_issue",
            "classification": "write",
            "requiredScopes": ["issues:write"],
            "timeoutSeconds": 30,
            "idempotency": "adapter_key",
            "maxResultBytes": 262144,
        },
        {
            "primitiveKey": "resource:workspace_guide",
            "allowed": True,
            "publicOperationId": "read_workspace_guide",
            "classification": "read",
            "requiredScopes": ["workspace:read"],
            "resource": {
                "allowedMimeTypes": ["text/plain", "text/markdown"],
                "maxBytes": 262144,
            },
        },
        {
            "primitiveKey": "resource_template:issue_document",
            "allowed": True,
            "publicOperationId": "read_issue_document",
            "classification": "read",
            "requiredScopes": ["issues:read"],
            "resource": {
                "allowedMimeTypes": ["text/plain", "text/markdown"],
                "maxBytes": 262144,
            },
        },
        {
            "primitiveKey": "prompt:triage_issue",
            "allowed": True,
            "publicOperationId": "get_triage_prompt",
            "classification": "read",
            "requiredScopes": ["issues:read"],
            "prompt": {
                "allowedRoles": ["user", "assistant"],
                "allowedEmbeddedMimeTypes": ["text/plain", "text/markdown"],
                "maxBytes": 262144,
            },
        },
    ]

    return {
        "catalog_version": "1.0.0",
        "server": {
            "id": "project-system",
            "endpoint": "https://mcp.example.test/mcp",
            "authProfile": "delegated_oauth",
        },
        "protocol_version": "2026-07-28",
        "discovered_at": "2026-10-04T18:00:00.000Z",
        "published_at": "2026-10-04T18:05:00.000Z",
        "primitives": primitives,
        "policies": policies,
    }


def discovery_manifest() -> dict[str, Any]:
    fixture = phase_one_fixture()
    return {
        "protocolVersion": fixture["protocol_version"],
        "serverInfo": {"name": "Project System Fixture", "version": "1.0.0"},
        "capabilities": {
            "tools": True,
            "resources": True,
            "prompts": True,
        },
        "primitives": fixture["primitives"],
    }
