"""The owner password: chosen by the owner, compared exactly, kept only as a salted hash."""

from __future__ import annotations

import pytest

from muse_mcp_adapter.chat_setup import owner


def _stored(password: str) -> tuple[str, str]:
    salt = owner.new_password_salt()
    return owner.hash_password(password, salt), salt


def test_a_password_matches_only_exactly() -> None:
    expected, salt = _stored("Correct Horse Battery")
    assert owner.matches("Correct Horse Battery", salt, expected)
    assert not owner.matches("correct horse battery", salt, expected)  # case matters
    assert not owner.matches("CorrectHorseBattery", salt, expected)  # spaces matter
    assert not owner.matches("Correct Horse Battery ", salt, expected)
    assert not owner.matches("", salt, expected)


def test_a_generated_code_still_ignores_case_spaces_and_dashes() -> None:
    code = owner.new_owner_code()
    salt = owner.new_salt()
    expected = owner.hash_code(code, salt)
    assert owner.matches(code.lower().replace("-", " "), salt, expected)


def test_the_two_kinds_do_not_mix() -> None:
    code_hash, code_salt = owner.hash_code("ABCD-EFGH-JKLM", "00" * 16), "00" * 16
    assert not owner.matches("ABCD-EFGH-JKLM", owner.new_password_salt(), code_hash)
    assert owner.matches("ABCD-EFGH-JKLM", code_salt, code_hash)


@pytest.mark.parametrize("bad", ["short", "x" * 9, "x" * 201, "has\nnewline in it", "tab\tin it!!"])
def test_weak_or_odd_passwords_are_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        owner.check_password(bad)


def test_a_reasonable_password_is_accepted() -> None:
    assert owner.check_password("a long enough phrase, with spaces") == (
        "a long enough phrase, with spaces"
    )


def test_each_password_gets_its_own_salt() -> None:
    assert owner.new_password_salt() != owner.new_password_salt()
    assert owner.new_password_salt().startswith(owner.PASSWORD_MARK)
