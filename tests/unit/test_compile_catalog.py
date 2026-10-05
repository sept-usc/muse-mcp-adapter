import pytest

from muse_mcp_adapter.control_plane.compile_catalog import compile_catalog
from muse_mcp_adapter.domain.errors import CatalogValidationError
from tests.fixtures.phase_one import phase_one_fixture


def test_compiler_publishes_an_immutable_allowlisted_catalog() -> None:
    input_data = phase_one_fixture()
    input_data["policies"][1]["allowed"] = False

    catalog = compile_catalog(**input_data)

    assert len(catalog.primitives) == 4
    assert all(item.mcp_name != "create_issue" for item in catalog.primitives)
    assert catalog.catalog_digest.startswith("sha256:")
    with pytest.raises(TypeError):
        catalog.server["id"] = "changed"  # type: ignore[index]


def test_compiler_rejects_duplicate_public_operation_ids() -> None:
    input_data = phase_one_fixture()
    input_data["policies"][1]["publicOperationId"] = "find_issue"

    with pytest.raises(CatalogValidationError) as error:
        compile_catalog(**input_data)

    assert "Duplicate publicOperationId: find_issue" in error.value.issues


def test_compiler_rejects_policy_for_unknown_primitive() -> None:
    input_data = phase_one_fixture()
    input_data["policies"].append(
        {
            "primitiveKey": "tool:unknown",
            "allowed": True,
            "publicOperationId": "unknown_tool",
            "classification": "read",
            "requiredScopes": [],
        }
    )

    with pytest.raises(CatalogValidationError) as error:
        compile_catalog(**input_data)

    assert "Policy references unknown primitive tool:unknown" in error.value.issues
