from copy import deepcopy

from muse_mcp_adapter.control_plane.compile_catalog import compile_catalog
from muse_mcp_adapter.drift.detect_drift import detect_drift
from tests.fixtures.phase_one import phase_one_fixture


def test_discovery_to_publication_covers_all_primitives_and_fails_closed_on_drift() -> None:
    discovery = phase_one_fixture()
    active_catalog = compile_catalog(**discovery)

    assert {item.primitive_type for item in active_catalog.primitives} == {
        "tool",
        "resource",
        "resource_template",
        "prompt",
    }

    later = deepcopy(discovery["primitives"])
    write_tool = next(item for item in later if item["name"] == "create_issue")
    write_tool["inputSchema"]["required"].append("projectId")
    write_tool["inputSchema"]["properties"]["projectId"] = {"type": "string"}
    later.append(
        {
            "type": "tool",
            "name": "delete_project",
            "inputSchema": {
                "type": "object",
                "properties": {"projectId": {"type": "string"}},
                "required": ["projectId"],
            },
            "annotations": {"destructiveHint": True},
            "requiredScopes": ["projects:delete"],
        }
    )

    drift = detect_drift(active_catalog, later)

    assert any(
        event.primitive_key == "tool:create_issue"
        and event.reason == "input_schema_changed"
        and event.severity == "block"
        for event in drift
    )
    assert any(
        event.primitive_key == "tool:delete_project"
        and event.reason == "primitive_added"
        and event.severity == "review"
        for event in drift
    )
    assert len(active_catalog.primitives) == 5
    assert all(item.mcp_name != "delete_project" for item in active_catalog.primitives)
