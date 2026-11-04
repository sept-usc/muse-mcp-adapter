"""Limits on the values an operation may be called with, one case per way a value can be written.

A limit is only worth something if it holds however the value is spelled. Each case here is a
spelling, and the answer the limit must give.
"""

from __future__ import annotations

import pytest

from muse_mcp_adapter.policy.argument_limits import (
    argument_texts,
    describe_limit,
    literal_values,
    no_looser,
    scalar_text,
    smuggling,
    undeclared,
    values_pattern,
    violation,
)

OWNER = values_pattern(["Kishore712"])
PLACE = r"https://learn\.microsoft\.com/azure/.*"


# --- how a value is read -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (True, "true"),
        (False, "false"),
        (0, "0"),
        (42, "42"),
        (-7, "-7"),
        (1.5, "1.5"),
        (1.0, "1.0"),
        ("text", "text"),
        ("", ""),
    ],
)
def test_a_value_is_compared_as_the_text_a_caller_wrote(value: object, text: str) -> None:
    assert scalar_text(value) == text  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "texts"),
    [
        ("a", ["a"]),
        (3, ["3"]),
        (False, ["false"]),
        (2.5, ["2.5"]),
        (["a", 1, True], ["a", "1", "true"]),
        ([], []),
        (("a", "b"), ["a", "b"]),
        ({"a": 1}, None),
        ([["a"]], None),
        (["a", {"b": 1}], None),
        (["a", None], None),
        (None, None),
        (b"bytes", None),
        (bytearray(b"x"), None),
    ],
)
def test_only_a_value_or_a_list_of_values_can_be_held_to_a_limit(
    value: object, texts: list[str] | None
) -> None:
    assert argument_texts(value) == texts


# --- a list of values --------------------------------------------------------------------


@pytest.mark.parametrize(
    "values",
    [
        ["ENG"],
        ["ENG", "OPS"],
        ["a.b", "c|d", "(e)", "f*", "g\\h", "i[j]", "k+l", "m?n", "o^p$q"],
        ["with space", "ünïcode"],
        ["1", "true"],
    ],
)
def test_a_list_of_values_reads_back_exactly_as_written(values: list[str]) -> None:
    assert literal_values(values_pattern(values)) == values


@pytest.mark.parametrize(
    "pattern",
    [
        "ENG",  # no wrapper
        "(?:ENG)(?:OPS)",  # two groups
        "(?:E.G)",  # a wildcard, not a letter
        "(?:ENG|)",  # an empty alternative
        "(?:|ENG)",
        "(?:EN+G)",
        "(?:ENG)+",
        ".*",
        "",
    ],
)
def test_anything_but_a_plain_list_of_values_is_not_read_as_one(pattern: str) -> None:
    assert literal_values(pattern) is None


@pytest.mark.parametrize(
    ("pattern", "words"),
    [
        (values_pattern(["ENG"]), "ENG"),
        (values_pattern(["ENG", "OPS"]), "one of ENG, OPS"),
        ("[a-z]+", "values matching [a-z]+"),
        (PLACE, f"values matching {PLACE}"),
    ],
)
def test_a_limit_is_put_in_words(pattern: str, words: str) -> None:
    assert describe_limit(pattern) == words


@pytest.mark.parametrize(
    ("old", "new", "narrower"),
    [
        (OWNER, OWNER, True),
        (values_pattern(["a", "b", "c"]), values_pattern(["a", "b"]), True),
        (values_pattern(["a", "b"]), values_pattern(["b", "a"]), True),
        (values_pattern(["a"]), values_pattern(["a", "b"]), False),
        (values_pattern(["a"]), values_pattern(["b"]), False),
        (values_pattern(["a"]), "[a-z]+", False),  # cannot be proved, so it is not accepted
        ("[a-z]+", values_pattern(["a"]), False),  # nor the other way round
        ("[a-z]+", "[a-z]+", True),  # the same text is the same limit
        ("[a-z]+", "[a-z]{1,3}", False),
    ],
)
def test_a_limit_is_no_wider_only_when_that_can_be_proved(
    old: str, new: str, narrower: bool
) -> None:
    assert no_looser(old, new) is narrower


# --- a call against a limit --------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "refused"),
    [
        ("Kishore712", False),
        (["Kishore712"], False),
        (["Kishore712", "Kishore712"], False),
        ("kishore712", True),  # the case matters
        ("Kishore71", True),
        ("Kishore7120", True),
        ("Kishore712\n", True),  # a trailing newline is not part of the value
        (" Kishore712", True),
        ("Kishore712 ", True),
        ("", True),
        ("Kishore712/../other", True),
        (["Kishore712", "other"], True),  # every item must be allowed
        ([], True),  # an empty list says nothing
        ({"owner": "Kishore712"}, True),  # not a value
        (None, True),
        ([["Kishore712"]], True),
    ],
)
def test_a_value_is_allowed_only_if_the_whole_of_it_is_one_of_the_values(
    value: object, refused: bool
) -> None:
    problem = violation({"owner": OWNER}, {"owner": value})
    assert (problem is not None) is refused


def test_the_limited_argument_must_be_there() -> None:
    problem = violation({"owner": OWNER}, {"repo": "x"})
    assert problem is not None and "required" in problem and "Kishore712" in problem


def test_every_limit_on_an_operation_is_checked() -> None:
    limits = {"owner": OWNER, "repo": values_pattern(["mcp-adapter-test-dummy"])}
    assert violation(limits, {"owner": "Kishore712", "repo": "mcp-adapter-test-dummy"}) is None
    assert violation(limits, {"owner": "Kishore712", "repo": "other"}) is not None
    assert violation(limits, {"owner": "other", "repo": "mcp-adapter-test-dummy"}) is not None
    assert violation(limits, {"owner": "Kishore712"}) is not None


def test_no_limits_means_nothing_is_refused() -> None:
    assert violation({}, {"anything": object()}) is None


def test_an_argument_that_is_not_limited_is_not_looked_at() -> None:
    assert violation({"owner": OWNER}, {"owner": "Kishore712", "page": {"any": ["thing"]}}) is None


# --- a value that hides what it says -----------------------------------------------------


@pytest.mark.parametrize(
    ("value", "hidden"),
    [
        ("https://learn.microsoft.com/azure/functions", None),
        ("https://learn.microsoft.com/azure/", None),
        ("https://learn.microsoft.com/azure/a.b/c.d", None),  # dots inside a name are fine
        ("https://learn.microsoft.com/azure/a\tb", None),  # a tab is not a control character here
        ("https://learn.microsoft.com/azure/../dotnet/", "a . or .. path segment"),
        ("https://learn.microsoft.com/azure/./x", "a . or .. path segment"),
        ("https://learn.microsoft.com/azure/.", "a . or .. path segment"),
        ("https://learn.microsoft.com/azure/...", "a . or .. path segment"),
        ("https://learn.microsoft.com/azure/a/..", "a . or .. path segment"),
        ("https://learn.microsoft.com/azure/a\\b", "a backslash"),
        ("https://learn.microsoft.com/azure/..\\x", "a backslash"),
        ("https://learn.microsoft.com/azure/%2e%2e/x", "a . or .. path segment"),
        ("https://learn.microsoft.com/azure/%2E%2E/x", "a . or .. path segment"),
        ("https://learn.microsoft.com/azure/%252e%252e/x", "a . or .. path segment"),
        ("https://learn.microsoft.com/azure/%25252e%25252e/x", "a . or .. path segment"),
        ("https://learn.microsoft.com/azure/a%2Fb", "an encoded dot or slash"),
        ("https://learn.microsoft.com/azure/a%2fb", "an encoded dot or slash"),
        ("https://learn.microsoft.com/azure/a%2eb", "an encoded dot or slash"),
        ("https://learn.microsoft.com/azure/a%5Cb", "a backslash"),
        ("https://learn.microsoft.com/azure/．．/b", "a . or .. path segment"),
        ("https://learn.microsoft.com/azure/a\x00b", "control characters"),
        ("https://learn.microsoft.com/azure/a\x1fb", "control characters"),
        ("https://learn.microsoft.com/azure/a\x7fb", "control characters"),
        ("https://learn.microsoft.com/azure/%00", "control characters"),
    ],
)
def test_a_value_that_only_passes_because_of_what_it_hides_is_refused(
    value: str, hidden: str | None
) -> None:
    assert smuggling(PLACE, value) == hidden
    problem = violation({"url": PLACE}, {"url": value})
    assert (problem is None) is (hidden is None)
    if hidden is not None:
        assert problem is not None and hidden in problem


def test_a_value_outside_the_pattern_is_refused_before_anything_else() -> None:
    problem = violation({"url": PLACE}, {"url": "https://example.com/azure/x"})
    assert problem is not None and "limited by the owner" in problem


@pytest.mark.parametrize(
    ("value", "hidden"),
    [
        ("abc", None),
        ("../x", None),  # no place named, so only control characters matter
        ("a\x01b", "control characters"),
        ("a%00b", "control characters"),
    ],
)
def test_a_pattern_that_names_no_place_only_refuses_control_characters(
    value: str, hidden: str | None
) -> None:
    assert smuggling(".*", value) == hidden


def test_a_fixed_list_needs_no_such_check() -> None:
    assert smuggling(OWNER, "..") is None
    assert smuggling(OWNER, "a\x00b") is None


# --- which arguments a call may carry ----------------------------------------------------


@pytest.mark.parametrize(
    ("schema", "arguments", "extra"),
    [
        ({"properties": {"owner": {}, "repo": {}}}, {"owner": 1, "repo": 2}, []),
        ({"properties": {"owner": {}}}, {"owner": 1, "alias": 2, "also": 3}, ["alias", "also"]),
        ({"properties": {"owner": {}}, "additionalProperties": False}, {"x": 1}, ["x"]),
        ({"properties": {"owner": {}}, "additionalProperties": True}, {"x": 1}, []),
        ({"properties": {"owner": {}}, "additionalProperties": {"type": "string"}}, {"x": 1}, []),
        ({"properties": {}}, {"x": 1}, []),  # declares nothing, so nothing to compare with
        ({}, {"x": 1}, []),
        ("not a schema", {"x": 1}, []),
        (None, {"x": 1}, []),
        ({"properties": "nope"}, {"x": 1}, []),
    ],
)
def test_a_limited_operation_takes_only_what_it_declares(
    schema: object, arguments: dict[str, object], extra: list[str]
) -> None:
    assert undeclared(schema, arguments) == extra
