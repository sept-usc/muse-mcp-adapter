from muse_mcp_adapter.policy.validate_policy import validate_exposure_policy


def test_resource_projection_must_remain_read_only() -> None:
    primitive: dict[str, object] = {
        "type": "resource",
        "name": "guide",
        "uri": "mcp://guide",
        "requiredScopes": [],
    }
    policy: dict[str, object] = {
        "primitiveKey": "resource:guide",
        "allowed": True,
        "publicOperationId": "read_guide",
        "classification": "write",
        "requiredScopes": [],
        "resource": {"allowedMimeTypes": ["text/plain"], "maxBytes": 1024},
    }

    issues = validate_exposure_policy(primitive, policy)

    assert any("must be classified as read" in issue for issue in issues)


def test_policy_must_include_every_scope_required_by_discovery() -> None:
    primitive: dict[str, object] = {
        "type": "tool",
        "name": "find_issue",
        "inputSchema": {"type": "object"},
        "requiredScopes": ["issues:read"],
    }
    policy: dict[str, object] = {
        "primitiveKey": "tool:find_issue",
        "allowed": True,
        "publicOperationId": "find_issue",
        "classification": "read",
        "requiredScopes": [],
    }

    issues = validate_exposure_policy(primitive, policy)

    assert any("missing required scope" in issue for issue in issues)
