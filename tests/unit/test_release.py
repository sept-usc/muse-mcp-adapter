# pyright: basic
"""Sealing results and opening arguments: the policy applied to plain data."""

import json

import pytest

from muse_mcp_adapter.data_plane.release import (
    FieldRule,
    ReleasePolicy,
    contains_handle,
    open_arguments,
    parse_path,
    redact,
    seal_result,
)
from muse_mcp_adapter.domain.errors import HandleNotAllowedError, HandleUnknownError

NOW = "2026-10-26T12:00:00Z"


class FakeVault:
    """A vault that numbers its handles so tests can read them."""

    def __init__(self) -> None:
        self.items: dict[str, tuple[str, str]] = {}

    def handle_for(self, connection_id: str, label: str, value: str, now: str) -> str:
        for key, (existing_label, existing_value) in self.items.items():
            if (existing_label, existing_value) == (label, value):
                return f"[{label}:{key}]"
        key = "abcdefg" + "234567"[len(self.items) % 6]
        self.items[key] = (label, value)
        return f"[{label}:{key}]"

    def lookup(self, connection_id: str, handle_id: str, now: str) -> tuple[str, str] | None:
        return self.items.get(handle_id)


class FakeLedger:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def record(self, kind, *, connection_id, operation, label, detail, now) -> None:
        self.rows.append((kind, label, detail))


def rules(*items: tuple[str, str, str]) -> ReleasePolicy:
    return ReleasePolicy(
        fields=tuple(FieldRule(parse_path(p), label, rung) for p, label, rung in items)
    )


def seal(policy, structured, texts=(), vault=None, ledger=None):
    return seal_result(
        policy, structured, list(texts), vault=vault or FakeVault(), ledger=ledger,
        connection_id="c1", operation="op", now=NOW,
    )  # fmt: skip


def body_of(sealed):
    assert sealed.structured is not None
    return sealed.structured


CONTACTS = {
    "contacts": [
        {
            "id": "c-1",
            "name": "Dana",
            "email": "dana@x.test",
            "phone": "+1 555 0101",
            "notes": "Allergic",
        },
        {"id": "c-2", "name": "Lee", "email": "lee@x.test", "phone": "+1 555 0102", "notes": ""},
    ],
    "total": 2,
}


def test_paths_are_parsed_and_bad_ones_refused() -> None:
    assert parse_path("$.contacts[*].email") == ("contacts", "*", "email")
    assert parse_path("$.body") == ("body",)
    assert parse_path("$[*]") == ("*",)
    for bad in ("contacts.email", "$..email", "$.a[0]", "$.a b", "", "$.a.", "$.a[*"):
        with pytest.raises(ValueError):
            parse_path(bad)


def test_a_handle_replaces_the_value_and_the_same_value_gets_the_same_handle() -> None:
    sealed = seal(rules(("$.contacts[*].email", "contact.email", "handle")), CONTACTS)
    emails = [c["email"] for c in body_of(sealed)["contacts"]]
    assert all(e.startswith("[contact.email:") and e.endswith("]") for e in emails)
    assert emails[0] != emails[1]
    again = seal(
        rules(("$.contacts[*].email", "contact.email", "handle")), CONTACTS, vault=FakeVault()
    )
    assert [c["email"] for c in body_of(again)["contacts"]] == emails  # stable
    assert sealed.sealed == 2


def test_other_fields_are_left_alone() -> None:
    sealed = seal(rules(("$.contacts[*].email", "contact.email", "handle")), CONTACTS)
    first = body_of(sealed)["contacts"][0]
    assert first["name"] == "Dana" and first["phone"] == "+1 555 0101" and first["id"] == "c-1"
    assert body_of(sealed)["total"] == 2


def test_a_withheld_field_is_gone_not_blanked() -> None:
    sealed = seal(rules(("$.contacts[*].notes", "contact.notes", "withhold")), CONTACTS)
    assert all("notes" not in c for c in body_of(sealed)["contacts"])
    assert sealed.withheld == 2


def test_a_full_rung_changes_nothing_and_a_missing_path_is_ignored() -> None:
    policy = rules(
        ("$.contacts[*].phone", "contact.phone", "full"), ("$.nothing.here", "x", "handle")
    )
    sealed = seal(policy, CONTACTS)
    assert sealed.structured == CONTACTS and sealed.sealed == 0


def test_handles_work_inside_lists_of_strings() -> None:
    data = {"messages": [{"to": ["a@x.test", "b@x.test"], "subject": "Hi"}]}
    sealed = seal(rules(("$.messages[*].to[*]", "contact.email", "handle")), data)
    assert all(t.startswith("[contact.email:") for t in body_of(sealed)["messages"][0]["to"])
    assert body_of(sealed)["messages"][0]["subject"] == "Hi"


def test_the_original_data_is_never_modified() -> None:
    original = json.loads(json.dumps(CONTACTS))
    seal(rules(("$.contacts[*].email", "contact.email", "handle")), CONTACTS)
    assert original == CONTACTS


def test_the_text_copy_of_a_structured_result_is_sealed_the_same_way() -> None:
    text = json.dumps(CONTACTS, indent=2)
    sealed = seal(
        rules(
            ("$.contacts[*].email", "contact.email", "handle"),
            ("$.contacts[*].notes", "contact.notes", "withhold"),
        ),
        CONTACTS,
        [text],
    )
    assert "dana@x.test" not in sealed.texts[0] and "Allergic" not in sealed.texts[0]
    assert json.loads(sealed.texts[0]) == sealed.structured


def test_other_free_text_gets_values_replaced_and_patterns_redacted() -> None:
    sealed = seal(
        rules(("$.contacts[*].email", "contact.email", "handle")),
        CONTACTS,
        ["Found dana@x.test; card 4111 1111 1111 1111 and SSN 900-12-3456."],
    )
    text = sealed.texts[0]
    assert "dana@x.test" not in text
    assert "[contact.email:" in text
    assert "[redacted:card]" in text and "[redacted:ssn]" in text


def test_short_values_are_not_substituted_into_free_text() -> None:
    data = {"code": "ab"}
    sealed = seal(rules(("$.code", "x.code", "handle")), data, ["about the ab test"])
    assert sealed.texts[0] == "about the ab test"


def test_redact_blanks_cards_by_checksum_and_not_other_numbers() -> None:
    text, hits = redact("pay 4111 1111 1111 1111 or order 1234 5678 9012 3456 now", ["card"])
    assert "[redacted:card]" in text and "1234 5678 9012 3456" in text
    assert hits == ["card"]
    text, _ = redact("4111-1111-1111-1111", ["card"])
    assert text == "[redacted:card]"


def test_redact_handles_ssn_and_iban_and_obeys_the_pattern_list() -> None:
    text, hits = redact("SSN 900-12-3456 and GB82 WEST 1234 5698 7654 32", ["ssn", "iban"])
    assert "[redacted:ssn]" in text and "[redacted:iban]" in text and len(hits) == 2
    text, hits = redact("SSN 900-12-3456", ["card"])
    assert text == "SSN 900-12-3456" and hits == []
    text, _ = redact("order 1900-12-34567", ["ssn"])  # part of a longer number: not an SSN
    assert "redacted" not in text


def test_a_redact_rung_cleans_one_field() -> None:
    data = {"body": "your card 4111 1111 1111 1111 is active", "subject": "4111 1111 1111 1111"}
    sealed = seal(rules(("$.body", "message.body", "redact")), data)
    assert body_of(sealed)["body"] == "your card [redacted:card] is active"
    assert body_of(sealed)["subject"] == "4111 1111 1111 1111"  # not covered by the rule
    assert sealed.redacted == 1


def test_everything_that_happened_is_in_the_ledger_without_the_values() -> None:
    ledger = FakeLedger()
    seal(
        rules(
            ("$.contacts[*].email", "contact.email", "handle"),
            ("$.contacts[*].notes", "contact.notes", "withhold"),
        ),
        CONTACTS,
        ledger=ledger,
    )
    kinds = [kind for kind, _, _ in ledger.rows]
    assert kinds.count("sealed") == 2 and kinds.count("withheld") == 2
    assert "dana@x.test" not in str(ledger.rows) and "Allergic" not in str(ledger.rows)


# --- on the way back in ---------------------------------------------------------------------


def opened(policy, arguments, vault, ledger=None):
    return open_arguments(
        policy, arguments, vault=vault, ledger=ledger, connection_id="c1", operation="send", now=NOW
    )


def setup():
    vault = FakeVault()
    token = vault.handle_for("c1", "contact.email", "dana@x.test", NOW)
    return (
        vault,
        token,
        ReleasePolicy(slots={"to": ("contact.email",), "attendees": ("contact.email",)}),
    )


def test_a_handle_is_filled_in_where_the_policy_allows() -> None:
    vault, token, policy = setup()
    ledger = FakeLedger()
    result = opened(policy, {"to": token, "body": "Hi"}, vault, ledger)
    assert result == {"to": "dana@x.test", "body": "Hi"}
    assert ledger.rows[0][0] == "rehydrated" and "dana@x.test" not in str(ledger.rows)


def test_a_handle_in_a_list_slot_is_filled_in() -> None:
    vault, token, policy = setup()
    assert opened(policy, {"attendees": [token, "x@y.test"]}, vault)["attendees"] == [
        "dana@x.test",
        "x@y.test",
    ]


def test_a_handle_inside_a_longer_text_is_filled_in_when_the_slot_allows() -> None:
    vault, token, policy = setup()
    assert opened(policy, {"to": f"Dana <{token}>"}, vault)["to"] == "Dana <dana@x.test>"


def test_a_handle_anywhere_else_is_refused_and_the_message_says_where_it_may_go() -> None:
    vault, token, policy = setup()
    ledger = FakeLedger()
    with pytest.raises(HandleNotAllowedError, match="only be used in: attendees, to"):
        opened(policy, {"to": "x@y.test", "body": f"her address is {token}"}, vault, ledger)
    assert ledger.rows[0][0] == "refused"


def test_a_handle_with_the_wrong_label_for_the_slot_is_refused() -> None:
    vault, _, policy = setup()
    phone = vault.handle_for("c1", "contact.phone", "+1 555 0101", NOW)
    with pytest.raises(HandleNotAllowedError):
        opened(policy, {"to": phone}, vault)


def test_a_tool_that_takes_no_handles_says_so() -> None:
    vault, token, _ = setup()
    with pytest.raises(HandleNotAllowedError, match="cannot be used as an argument"):
        opened(ReleasePolicy(), {"to": token}, vault)


def test_an_unknown_edited_or_expired_handle_is_refused() -> None:
    vault, token, policy = setup()
    with pytest.raises(HandleUnknownError):
        opened(policy, {"to": "[contact.email:zzzzzzzz]"}, vault)
    edited = token.replace("contact.email", "contact.phone")  # right id, wrong label
    with pytest.raises(HandleUnknownError):
        opened(policy, {"to": edited}, vault)


def test_ordinary_arguments_pass_through_untouched() -> None:
    vault, _, policy = setup()
    arguments = {"to": "a@b.test", "n": 3, "flag": True, "nested": {"k": ["v", 1]}}
    assert opened(policy, arguments, vault) == arguments


def test_nested_slots_use_a_dotted_path() -> None:
    vault, token, _ = setup()
    policy = ReleasePolicy(slots={"meta.who": ("contact.email",)})
    assert opened(policy, {"meta": {"who": token}}, vault) == {"meta": {"who": "dana@x.test"}}
    with pytest.raises(HandleNotAllowedError):
        opened(policy, {"meta": {"other": token}}, vault)


def test_contains_handle_looks_everywhere() -> None:
    assert contains_handle({"a": ["x", {"b": "see [contact.email:abcdefg2]"}]})
    assert not contains_handle({"a": ["x", {"b": "[not a handle]"}]})


def test_a_rule_that_matches_nothing_is_recorded_so_a_wrong_path_does_not_go_unnoticed() -> None:
    ledger = FakeLedger()
    seal(
        rules(
            ("$.contacts[*].email", "contact.email", "handle"),
            ("$.people[*].email", "contact.email", "handle"),  # no such list in the result
        ),
        CONTACTS,
        ledger=ledger,
    )
    unmatched = [row for row in ledger.rows if row[0] == "unmatched"]
    assert unmatched == [("unmatched", "contact.email", "no field matched $.people[*].email")]


def test_a_value_the_adapter_filled_in_is_sealed_again_if_the_tool_repeats_it() -> None:
    result = {"sent": True, "to": "dana@x.test"}
    sealed = seal_result(
        ReleasePolicy(),
        result,
        [json.dumps(result)],
        vault=FakeVault(),
        ledger=None,
        connection_id="c1",
        operation="send",
        now=NOW,
        echoes={"dana@x.test": "[contact.email:abcdefg2]"},
    )
    assert body_of(sealed) == {"sent": True, "to": "[contact.email:abcdefg2]"}
    assert "dana@x.test" not in sealed.texts[0]
