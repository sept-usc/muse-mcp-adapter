# pyright: basic
"""Reading ownerControls from a policy, and the argument paths the gate looks at."""

from collections.abc import Mapping
from typing import cast

import pytest

from muse_mcp_adapter.catalog.canonical import freeze_json
from muse_mcp_adapter.data_plane.owner_gate import (
    DEFAULT_UNTRUSTED_WINDOW_SECONDS,
    OwnerControls,
    check_arguments,
    controls_of,
    values_at,
)
from muse_mcp_adapter.domain.errors import ArgumentNotAllowedError
from muse_mcp_adapter.domain.models import PublishedPrimitive


def primitive(policy: dict, classification: str = "write") -> PublishedPrimitive:
    return PublishedPrimitive(
        primitive_key="tool:t",
        primitive_type="tool",
        mcp_name="t",
        public_operation_id="t_op",
        classification=classification,
        definition_digest="d",
        contract_digest="c",
        definition=cast(Mapping[str, object], freeze_json({})),
        policy=cast(Mapping[str, object], freeze_json(policy)),
    )


def test_a_policy_without_controls_asks_the_owner_for_every_write() -> None:
    assert controls_of(primitive({})) == OwnerControls()
    assert controls_of(primitive({})).approval == "always"


def test_controls_are_read_from_the_stored_policy() -> None:
    controls = controls_of(
        primitive(
            {
                "ownerControls": {
                    "approval": "risk",
                    "recipients": ["to", "cc"],
                    "knownRecipients": ["*@home.test"],
                    "untrusted": True,
                    "untrustedWindowSeconds": 60,
                    "arguments": {"to": ["a@b\\.test"]},
                }
            }
        )
    )
    assert controls.approval == "risk" and controls.recipients == ("to", "cc")
    assert controls.known_recipients == ("*@home.test",)
    assert controls.untrusted and controls.untrusted_window_seconds == 60
    assert controls.arguments == {"to": ("a@b\\.test",)}


def test_a_bad_window_falls_back_to_the_default() -> None:
    for bad in (0, -5, True, "x", None):
        controls = controls_of(primitive({"ownerControls": {"untrustedWindowSeconds": bad}}))
        assert controls.untrusted_window_seconds == DEFAULT_UNTRUSTED_WINDOW_SECONDS


def test_values_are_found_at_a_path_and_lists_are_flattened() -> None:
    args = {
        "to": "a@b.test",
        "cc": ["c@d.test", "e@f.test", 7],
        "meta": {"who": [{"mail": "g@h.test"}, {"mail": "i@j.test"}, {"other": 1}]},
    }
    assert values_at(args, "to") == ["a@b.test"]
    assert values_at(args, "cc") == ["c@d.test", "e@f.test"]  # the number is not text
    assert values_at(args, "meta.who.mail") == ["g@h.test", "i@j.test"]
    assert values_at(args, "missing") == []
    assert values_at(args, "to.deeper") == []
    assert values_at({"to": 5}, "to") == []


def test_a_value_outside_the_limit_is_refused_and_named() -> None:
    thing = primitive({"ownerControls": {"arguments": {"to": ["[^@]+@home\\.test"]}}})
    check_arguments(thing, {"to": "lee@home.test"})
    check_arguments(thing, {"to": "LEE@HOME.TEST"})  # case does not matter
    check_arguments(thing, {})  # an absent argument is not a violation
    with pytest.raises(ArgumentNotAllowedError, match="evil@x.test"):
        check_arguments(thing, {"to": "evil@x.test"})


def test_the_limit_must_match_the_whole_value() -> None:
    thing = primitive({"ownerControls": {"arguments": {"to": ["lee@home\\.test"]}}})
    with pytest.raises(ArgumentNotAllowedError):
        check_arguments(thing, {"to": "lee@home.test.evil.test"})
    with pytest.raises(ArgumentNotAllowedError):
        check_arguments(thing, {"to": "x lee@home.test"})


def test_every_value_in_a_list_must_be_inside_the_limit() -> None:
    thing = primitive({"ownerControls": {"arguments": {"cc": ["[^@]+@home\\.test"]}}})
    check_arguments(thing, {"cc": ["a@home.test", "b@home.test"]})
    with pytest.raises(ArgumentNotAllowedError, match="z@evil.test"):
        check_arguments(thing, {"cc": ["a@home.test", "z@evil.test"]})


def test_a_very_long_value_is_shortened_in_the_message() -> None:
    thing = primitive({"ownerControls": {"arguments": {"to": ["x"]}}})
    with pytest.raises(ArgumentNotAllowedError) as caught:
        check_arguments(thing, {"to": "y" * 500})
    assert len(str(caught.value)) < 300
