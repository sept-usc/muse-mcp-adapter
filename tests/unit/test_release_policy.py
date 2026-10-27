# pyright: basic
"""The release block of a policy: what publishing accepts, and what the published spec says."""

from collections.abc import Mapping
from typing import cast

from muse_mcp_adapter.application.openapi import _allow_sealed_values
from muse_mcp_adapter.catalog.canonical import freeze_json
from muse_mcp_adapter.domain.models import PublishedPrimitive
from muse_mcp_adapter.policy.validate_policy import validate_exposure_policy

TOOL = {
    "type": "tool",
    "name": "mail_send",
    "inputSchema": {"type": "object"},
    "annotations": {"readOnlyHint": False},
}


def policy(release: object) -> dict:
    return {
        "primitiveKey": "tool:mail_send",
        "allowed": True,
        "publicOperationId": "mail_send",
        "classification": "write",
        "maxResultBytes": 1000,
        "release": release,
    }


def issues(release: object) -> list[str]:
    return validate_exposure_policy(TOOL, policy(release))


GOOD = {
    "fields": [
        {"path": "$.contacts[*].email", "label": "contact.email", "rung": "handle"},
        {"path": "$.body", "label": "message.body", "rung": "redact"},
        {"path": "$.contacts[*].notes", "label": "contact.notes", "rung": "withhold"},
        {"path": "$.name", "label": "contact.name", "rung": "full"},
    ],
    "slots": {"to": ["contact.email"], "attendees": ["contact.email", "contact.alias"]},
    "patterns": ["card", "ssn"],
}


def test_a_complete_release_block_is_accepted() -> None:
    assert issues(GOOD) == []
    assert issues({}) == []  # an empty block is a block that does nothing


def test_a_tool_without_one_is_unaffected() -> None:
    plain = policy(None)
    del plain["release"]
    assert validate_exposure_policy(TOOL, plain) == []


def test_unknown_settings_are_refused() -> None:
    assert any("unknown setting: surprise" in i for i in issues({"surprise": 1}))


def test_a_path_must_be_a_supported_one() -> None:
    for bad in ("contacts.email", "$..email", "$.a[0]", "$.a b", "", 5, None):
        found = issues({"fields": [{"path": bad, "label": "a.b", "rung": "handle"}]})
        assert any("not a supported path" in i for i in found), bad


def test_a_label_must_look_like_a_dotted_name() -> None:
    for bad in ("email", "Contact.Email", "a..b", "a.b c", "", 5):
        found = issues({"fields": [{"path": "$.a", "label": bad, "rung": "handle"}]})
        assert any("must look like a.b" in i for i in found), bad


def test_a_rung_must_be_one_of_the_four() -> None:
    found = issues({"fields": [{"path": "$.a", "label": "a.b", "rung": "hide"}]})
    assert any("rung" in i for i in found)


def test_a_field_entry_must_be_an_object_and_the_list_a_list() -> None:
    assert any("not an object" in i for i in issues({"fields": ["$.a"]}))
    assert any("must be a list" in i for i in issues({"fields": "x"}))


def test_slots_and_patterns_are_checked() -> None:
    assert any("release.slots" in i for i in issues({"slots": {"to": ["email"]}}))
    assert any("release.slots" in i for i in issues({"slots": {"to": "contact.email"}}))
    assert any("release.slots" in i for i in issues({"slots": ["to"]}))
    assert any("release.patterns" in i for i in issues({"patterns": ["card", "passport"]}))
    assert any("must be an object" in i for i in issues("yes"))


def primitive(schema: dict) -> PublishedPrimitive:
    return PublishedPrimitive(
        primitive_key="tool:mail_send",
        primitive_type="tool",
        mcp_name="mail_send",
        public_operation_id="mail_send",
        classification="write",
        definition_digest="d",
        contract_digest="c",
        definition=cast(Mapping[str, object], freeze_json({"inputSchema": schema})),
        policy=cast(
            Mapping[str, object],
            freeze_json({"release": {"slots": {"to": ["contact.email"], "cc": ["contact.email"]}}}),
        ),
    )


def test_the_published_spec_drops_typing_that_would_reject_a_sealed_value() -> None:
    schema = {
        "type": "object",
        "properties": {
            "to": {"type": "string", "format": "email", "maxLength": 80, "description": "Who to"},
            "cc": {"type": "array", "items": {"type": "string", "format": "email"}},
            "subject": {"type": "string", "maxLength": 100},
        },
    }
    loosened: dict = _allow_sealed_values(schema, primitive(schema))  # type: ignore[assignment]
    to = loosened["properties"]["to"]
    assert "format" not in to and "maxLength" not in to and to["type"] == "string"
    assert to["description"].startswith("Who to") and "sealed value" in to["description"]
    assert "format" not in loosened["properties"]["cc"]["items"]
    assert loosened["properties"]["subject"] == {"type": "string", "maxLength": 100}  # untouched


def test_a_tool_with_no_slots_publishes_its_schema_unchanged() -> None:
    schema = {"type": "object", "properties": {"to": {"type": "string", "format": "email"}}}
    plain = primitive(schema)
    object.__setattr__(plain, "policy", cast(Mapping[str, object], freeze_json({})))
    unchanged: dict = _allow_sealed_values(schema, plain)  # type: ignore[assignment]
    assert unchanged["properties"]["to"]["format"] == "email"


def test_text_rules_are_checked() -> None:
    good = {
        "text": [{"find": "email", "label": "contact.email", "rung": "handle", "keep": ["a@b.c"]}]
    }
    assert issues(good) == []
    assert any(
        "release.text find" in i
        for i in issues({"text": [{"find": "name", "label": "a.b", "rung": "handle"}]})
    )
    assert any(
        "release.text label" in i
        for i in issues({"text": [{"find": "phone", "label": "phone", "rung": "handle"}]})
    )
    assert any(
        "release.text rung" in i
        for i in issues({"text": [{"find": "phone", "label": "a.b", "rung": "redact"}]})
    )
    assert any(
        "release.text keep" in i
        for i in issues(
            {"text": [{"find": "phone", "label": "a.b", "rung": "handle", "keep": "x"}]}
        )
    )
    assert any(
        "unknown setting: extra" in i
        for i in issues({"text": [{"find": "phone", "label": "a.b", "rung": "handle", "extra": 1}]})
    )
    assert any("release.text for" in i and "must be a list" in i for i in issues({"text": "email"}))
    assert any("not an object" in i for i in issues({"text": ["email"]}))


def test_a_regex_text_rule_needs_a_valid_short_pattern_and_only_it() -> None:
    def rule(**extra):
        return {"text": [{"find": "regex", "label": "a.b", "rung": "withhold", **extra}]}

    assert issues(rule(pattern="(?m)^Notes: .*$")) == []
    assert any("needs a pattern" in i for i in issues(rule()))
    assert any("not valid" in i for i in issues(rule(pattern="(")))
    assert any("over 200" in i for i in issues(rule(pattern="a" * 201)))
    assert any(
        "only goes with find: regex" in i
        for i in issues(
            {"text": [{"find": "email", "label": "a.b", "rung": "handle", "pattern": "x"}]}
        )
    )
