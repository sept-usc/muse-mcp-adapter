# pyright: basic
"""The policy examples in the owner-approval guide are valid policies, as written."""

import json
import re
from pathlib import Path

from muse_mcp_adapter.policy.validate_policy import validate_exposure_policy

GUIDE = Path(__file__).parents[2] / "docs" / "owner-approval.md"
TOOL = {"type": "tool", "name": "t", "inputSchema": {"type": "object"}, "annotations": {}}


def examples() -> list[dict]:
    blocks = re.findall(r"```json\n(.*?)```", GUIDE.read_text(encoding="utf-8"), re.DOTALL)
    return [json.loads(block) for block in blocks if '"primitiveKey"' in block]


def test_the_guide_has_the_two_policy_examples() -> None:
    assert [e["publicOperationId"] for e in examples()] == ["mail_send", "mail_read"]


def test_each_example_passes_the_publishing_checks() -> None:
    for policy in examples():
        name = policy["primitiveKey"].split(":", 1)[1]
        primitive = {
            **TOOL,
            "name": name,
            "annotations": {"readOnlyHint": policy["classification"] == "read"},
        }
        assert validate_exposure_policy(primitive, policy) == [], policy["primitiveKey"]


def test_the_notification_example_is_a_fragment_of_valid_configuration() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    block = next(
        b for b in re.findall(r"```json\n(.*?)```", text, re.DOTALL) if "notifications" in b
    )
    email = json.loads("{" + block + "}")["notifications"]["email"]
    assert {"host", "port", "from", "to", "usernameEnv", "passwordEnv"} <= set(email)


SEALED = Path(__file__).parents[2] / "docs" / "sealed-values.md"


def test_the_sealed_values_examples_pass_the_publishing_checks() -> None:
    text = SEALED.read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)```", text, re.DOTALL)
    policies = [json.loads(block) for block in blocks]
    assert [p["publicOperationId"] for p in policies] == [
        "contacts_search",
        "mail_send",
        "get_contact",
        "search_contacts",
    ]
    for policy in policies:
        name = policy["primitiveKey"].split(":", 1)[1]
        primitive = {
            **TOOL,
            "name": name,
            "annotations": {"readOnlyHint": policy["classification"] == "read"},
        }
        assert validate_exposure_policy(primitive, policy) == [], name
