from copy import deepcopy

from muse_mcp_adapter.control_plane.compile_catalog import compile_catalog
from muse_mcp_adapter.drift.detect_drift import detect_drift
from tests.fixtures.phase_one import phase_one_fixture


def test_drift_detector_classifies_schema_and_authorization_changes() -> None:
    input_data = phase_one_fixture()
    catalog = compile_catalog(**input_data)
    later = deepcopy(input_data["primitives"])
    later[0]["inputSchema"]["properties"]["teamId"] = {"type": "string"}
    later[0]["requiredScopes"].append("teams:read")

    events = detect_drift(catalog, later)

    assert any(event.drift_type == "schema" for event in events)
    assert any(event.drift_type == "authorization" for event in events)
    assert all(event.severity == "block" for event in events)


def test_new_primitives_require_review_and_removals_block() -> None:
    input_data = phase_one_fixture()
    catalog = compile_catalog(**input_data)
    later = deepcopy(input_data["primitives"][1:])
    later.append(
        {
            "type": "tool",
            "name": "new_tool",
            "inputSchema": {"type": "object"},
            "requiredScopes": [],
        }
    )

    events = detect_drift(catalog, later)
    added = next(event for event in events if event.primitive_key == "tool:new_tool")
    removed = next(event for event in events if event.primitive_key == "tool:find_issue")

    assert (added.drift_type, added.severity, added.reason) == (
        "capability",
        "review",
        "primitive_added",
    )
    assert (removed.severity, removed.reason) == ("block", "primitive_removed")
