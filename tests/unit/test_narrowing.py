"""What counts as making access smaller: the rule an agent is held to.

The agent may change a connection on its own only when the change gives nothing more. Each case
below is one way a change can differ from what is live, and the answer the rule must give.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from muse_mcp_adapter.application.control_plane_service import ControlPlaneService
from muse_mcp_adapter.discovery.client import ManifestDiscoveryClient
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from muse_mcp_adapter.policy.argument_limits import values_pattern
from muse_mcp_adapter.policy.narrowing import Exposure, classify_change, exposures_of
from tests.fixtures.phase_one import discovery_manifest, phase_one_fixture

KEY = "tool:list_issues"
SOON = "2026-10-05T12:00:00Z"
LATER = "2026-10-05T18:00:00Z"
ONE = values_pattern(["acme"])
EITHER = values_pattern(["acme", "globex"])


def _exposure(digest: str = "d1", /, **changes: object) -> Exposure:
    policy: dict[str, object] = {
        "allowed": True,
        "primitiveKey": KEY,
        "publicOperationId": "list_issues",
        "classification": "read",
        "requiredScopes": [],
        "description": "List issues.",
        "timeoutSeconds": 30,
        "maxResultBytes": 262144,
    }
    for name, value in changes.items():
        if value is None:
            policy.pop(name, None)
        else:
            policy[name] = value
    return Exposure(digest, policy)


def _only(exposure: Exposure) -> dict[str, Exposure]:
    return {KEY: exposure}


# --- the whole set of operations ---------------------------------------------------------


def test_the_same_exposure_is_no_change() -> None:
    assert classify_change(_only(_exposure()), _only(_exposure())) == "none"
    assert classify_change({}, {}) == "none"


def test_taking_something_away_is_narrowing() -> None:
    both = {KEY: _exposure(), "tool:get_issue": _exposure()}
    assert classify_change(both, {KEY: _exposure()}) == "narrowing"
    assert classify_change(both, {}) == "narrowing"


def test_putting_anything_new_in_is_widening() -> None:
    assert classify_change({}, _only(_exposure())) == "widening"
    assert (
        classify_change(_only(_exposure()), {KEY: _exposure(), "tool:x": _exposure()}) == "widening"
    )


def test_one_operation_getting_wider_makes_the_whole_change_wider() -> None:
    old = {KEY: _exposure(), "tool:get_issue": _exposure(timeoutSeconds=10)}
    new = {"tool:get_issue": _exposure(timeoutSeconds=60)}  # the other one was dropped
    assert classify_change(old, new) == "widening"


def test_a_server_that_redefined_an_operation_needs_the_owner_again() -> None:
    assert classify_change(_only(_exposure("d1")), _only(_exposure("d2"))) == "widening"


# --- the kind of operation ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("read", "read", "none"),
        ("read", "write", "narrowing"),
        ("read", "sensitive_write", "narrowing"),
        ("write", "sensitive_write", "narrowing"),
        ("write", "read", "widening"),
        ("sensitive_write", "write", "widening"),
        ("sensitive_write", "read", "widening"),
        ("read", "something_else", "widening"),
        ("something_else", "read", "widening"),
    ],
)
def test_a_more_careful_kind_is_not_wider_and_a_less_careful_one_is(
    old: str, new: str, expected: str
) -> None:
    assert (
        classify_change(_only(_exposure(classification=old)), _only(_exposure(classification=new)))
        == expected
    )


# --- timeouts and result sizes -----------------------------------------------------------


@pytest.mark.parametrize("field", ["timeoutSeconds", "maxResultBytes"])
@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (30, 10, "narrowing"),
        (30, 30, "none"),
        (30, 60, "widening"),
        (30, None, "widening"),  # taking a cap away
        (30, "10", "widening"),  # not a number
        (30, True, "widening"),  # a yes/no is not a number
        (30, 10.5, "widening"),
        (None, 10, "narrowing"),  # putting a cap where there was none
        (None, None, "none"),
        (None, "x", "widening"),
        (True, 10, "widening"),  # a damaged old value is not something to compare with
        ("30", 10, "widening"),
    ],
)
def test_a_smaller_cap_is_not_wider_and_a_larger_or_missing_one_is(
    field: str, old: object, new: object, expected: str
) -> None:
    before, after = _exposure(**{field: old}), _exposure(**{field: new})
    assert classify_change(_only(before), _only(after)) == expected


# --- when access ends --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (None, SOON, "narrowing"),  # giving it an end
        (LATER, SOON, "narrowing"),  # shortening it
        (SOON, SOON, "none"),
        (SOON, LATER, "widening"),  # extending it
        (SOON, None, "widening"),  # making it permanent
        (SOON, "garbage", "widening"),
        ("garbage", SOON, "widening"),  # an unreadable old end is not something to beat
        (None, None, "none"),
    ],
)
def test_an_earlier_end_is_not_wider_and_a_later_or_missing_one_is(
    old: str | None, new: str | None, expected: str
) -> None:
    assert (
        classify_change(_only(_exposure(expiresAt=old)), _only(_exposure(expiresAt=new)))
        == expected
    )


# --- limits on what an operation takes ---------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (None, {"owner": ONE}, "narrowing"),  # a first limit
        ({"owner": ONE}, {"owner": ONE}, "none"),
        ({"owner": EITHER}, {"owner": ONE}, "narrowing"),  # fewer allowed values
        ({"owner": ONE}, {"owner": EITHER}, "widening"),  # more allowed values
        ({"owner": ONE}, {"owner": ONE, "repo": ONE}, "narrowing"),  # one more argument limited
        ({"owner": ONE, "repo": ONE}, {"owner": ONE}, "widening"),  # one limit dropped
        ({"owner": ONE}, None, "widening"),  # every limit dropped
        ({"owner": ONE}, {"owner": "acme"}, "widening"),  # not provably the same
        ({"owner": ONE}, {"owner": ".*"}, "widening"),
        ({"owner": ONE}, {"owner": 5}, "widening"),  # not a pattern
        ("not a mapping", {"owner": ONE}, "widening"),
        ({"owner": ONE}, "not a mapping", "widening"),
    ],
)
def test_a_tighter_limit_is_not_wider_and_a_looser_or_missing_one_is(
    old: object, new: object, expected: str
) -> None:
    before = _exposure(argumentPatterns=old)
    after = _exposure(argumentPatterns=new)
    assert classify_change(_only(before), _only(after)) == expected


# --- everything else about the policy ----------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("description", "Changed words."),
        ("publicOperationId", "list_things"),
        ("requiredScopes", ["issues:write"]),
        ("allowed", False),
        ("somethingNew", 1),
    ],
)
def test_any_other_change_to_the_policy_needs_the_owner(field: str, value: object) -> None:
    assert classify_change(_only(_exposure()), _only(_exposure(**{field: value}))) == "widening"


def test_a_review_flag_alone_gives_nobody_more() -> None:
    reviewed = _exposure(readOnlyReviewed=True)
    assert classify_change(_only(_exposure()), _only(reviewed)) != "widening"


def test_narrowing_several_things_at_once_is_still_narrowing() -> None:
    old = _exposure(argumentPatterns={"owner": EITHER})
    new = _exposure(
        classification="write",
        timeoutSeconds=10,
        maxResultBytes=1024,
        expiresAt=SOON,
        argumentPatterns={"owner": ONE},
    )
    assert classify_change(_only(old), _only(new)) == "narrowing"


def test_narrowing_one_thing_while_widening_another_is_widening() -> None:
    old = _exposure(timeoutSeconds=30, maxResultBytes=1000)
    new = _exposure(timeoutSeconds=10, maxResultBytes=5000)
    assert classify_change(_only(old), _only(new)) == "widening"


# --- reading a published catalog ---------------------------------------------------------


def test_nothing_published_exposes_nothing() -> None:
    assert exposures_of(None) == {}


def test_a_published_catalog_is_read_as_plain_values(tmp_path: Path) -> None:
    with SqliteControlPlaneRepository(tmp_path / "adapter.db") as repository:
        repository.initialize()
        service = ControlPlaneService(repository)
        service.register_server(
            server_id="project-system",
            display_name="Project System",
            endpoint="https://mcp.example.test/mcp",
            auth_profile="delegated_oauth",
        )
        service.discover(
            "project-system",
            ManifestDiscoveryClient(discovery_manifest(), discovered_at="2026-10-04T18:01:00Z"),
        )
        policies: list[dict[str, Any]] = deepcopy(phase_one_fixture()["policies"])
        catalog = service.publish_catalog(
            server_id="project-system", catalog_version="1", policies=policies
        )

        found = exposures_of(catalog)

        assert set(found) == {item["primitiveKey"] for item in policies}
        issue = found["tool:find_issue"]
        assert issue.definition_digest == next(
            item.definition_digest
            for item in catalog.primitives
            if item.primitive_key == "tool:find_issue"
        )
        assert isinstance(issue.policy, dict)  # a plain copy, not the catalog's frozen view
        assert issue.policy["classification"] == "read"
        issue.policy["classification"] = "write"  # changing the copy changes nothing published
        assert exposures_of(catalog)["tool:find_issue"].policy["classification"] == "read"
        assert classify_change(exposures_of(catalog), exposures_of(catalog)) == "none"
