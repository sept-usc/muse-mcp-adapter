import pytest

from muse_mcp_adapter.catalog.canonical import canonical_json, digest


def test_digest_does_not_depend_on_object_key_order() -> None:
    left = {"z": 1, "nested": {"b": True, "a": "value"}}
    right = {"nested": {"a": "value", "b": True}, "z": 1}

    assert canonical_json(left) == canonical_json(right)
    assert digest(left) == digest(right)


def test_canonical_json_rejects_values_not_supported_by_json() -> None:
    with pytest.raises(TypeError, match=r"Unsupported value at \$\.unsafe"):
        canonical_json({"unsafe": object()})
