"""The owner can limit what a tool is called with, and every call is held to it."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from openapi_spec_validator import validate

from muse_mcp_adapter.application.openapi import compile_openapi
from muse_mcp_adapter.credentials.resolver import InMemoryCredentialResolver
from muse_mcp_adapter.data_plane.tool_service import COMPLETED, ToolDataPlaneService
from muse_mcp_adapter.domain.errors import RequestValidationError
from muse_mcp_adapter.domain.models import DataPlaneRequest
from muse_mcp_adapter.policy.argument_limits import (
    describe_limit,
    literal_values,
    no_looser,
    values_pattern,
    violation,
)
from muse_mcp_adapter.policy.validate_policy import validate_exposure_policy
from tests.fixtures.active_runtime import active_repository
from tests.fixtures.phase_one import phase_one_fixture
from tests.fixtures.tool_runtime import ConformanceToolClient

_NOW = "2026-10-04T18:05:00Z"


def _policies(limits: dict[str, str] | None) -> list[dict[str, Any]]:
    policies: list[dict[str, Any]] = copy.deepcopy(phase_one_fixture()["policies"])
    if limits is not None:
        for policy in policies:
            if policy["primitiveKey"] == "tool:find_issue":
                policy["argumentPatterns"] = limits
    return policies


def _service(repository: Any, client: ConformanceToolClient) -> ToolDataPlaneService:
    resolver = InMemoryCredentialResolver()
    resolver.register(
        local_identity_id="user-1",
        connection_id="connection-1",
        server_id="project-system",
        credential_ref="secret://project-system/user-1",
        access_token="token",
        scopes=frozenset({"issues:read", "issues:write"}),
    )
    ids = iter(f"operation-{number}" for number in range(100))
    return ToolDataPlaneService(
        repository,
        resolver,
        client,
        clock=lambda: _NOW,
        operation_id_factory=lambda: next(ids),
    )


def _find(value: object) -> DataPlaneRequest:
    return DataPlaneRequest(
        request_id="request-1",
        server_id="project-system",
        public_operation_id="find_issue",
        local_identity_id="user-1",
        connection_id="connection-1",
        arguments={"id": value},
    )


# --- the pure rule ---------------------------------------------------------------------------


def test_a_list_of_values_becomes_a_pattern_that_accepts_exactly_those() -> None:
    pattern = values_pattern(["ENG", "OPS-1", True, 3])

    assert pattern == r"(?:ENG|OPS\-1|true|3)"
    assert literal_values(pattern) == ["ENG", "OPS-1", "true", "3"]
    assert describe_limit(pattern) == "one of ENG, OPS-1, true, 3"
    assert describe_limit(values_pattern(["ENG"])) == "ENG"
    assert describe_limit("ISSUE-[0-9]+") == "values matching ISSUE-[0-9]+"


def test_a_call_is_held_to_every_limit_and_must_carry_the_limited_argument() -> None:
    limits = {"project": values_pattern(["ENG", "OPS"]), "id": "[0-9]+"}

    assert violation(limits, {"project": "ENG", "id": "12"}) is None
    assert violation(limits, {"project": ["ENG", "OPS"], "id": 12}) is None
    assert "limited by the owner to one of ENG, OPS" in str(
        violation(limits, {"project": "HR", "id": "12"})
    )
    assert violation(limits, {"project": ["ENG", "HR"], "id": "12"}) is not None  # all of them
    assert "is required" in str(violation(limits, {"id": "12"}))  # leaving it out is no way round
    assert "must be a value or a list" in str(violation(limits, {"project": {}, "id": "1"}))
    assert "must be a value or a list" in str(violation(limits, {"project": [], "id": "1"}))
    nested: dict[str, object] = {"project": [{}], "id": "1"}
    assert "must be a value or a list" in str(violation(limits, nested))
    assert violation(limits, {"project": "ENG ", "id": "12"}) is not None  # the whole value
    assert violation(limits, {"project": "xENG", "id": "12"}) is not None


def test_a_limit_never_widens_unless_it_is_the_same_or_a_part_of_the_old_list() -> None:
    both = values_pattern(["ENG", "OPS"])

    assert no_looser(both, both)
    assert no_looser(both, values_pattern(["ENG"]))
    assert not no_looser(values_pattern(["ENG"]), both)
    assert not no_looser(values_pattern(["ENG"]), values_pattern(["OPS"]))
    assert not no_looser("[A-Z]+", values_pattern(["ENG"]))  # cannot be proved, so refused
    assert not no_looser(both, "[A-Z]+")
    assert no_looser("[A-Z]+", "[A-Z]+")


# --- the runtime ----------------------------------------------------------------------------


def test_a_call_inside_the_limit_runs_and_one_outside_never_reaches_the_server(
    tmp_path: Path,
) -> None:
    with active_repository(tmp_path / "a.db", _policies({"id": "ISSUE-[0-9]+"})) as repository:
        client = ConformanceToolClient()
        service = _service(repository, client)

        assert service.execute(_find("ISSUE-42")).operation_status == COMPLETED
        for outside in ("OTHER-1", "ISSUE-42 ", "x ISSUE-42", ""):
            with pytest.raises(RequestValidationError, match="limited by the owner"):
                service.execute(_find(outside))
        with pytest.raises(RequestValidationError):  # not even the right kind of value
            service.execute(_find({"nested": "ISSUE-1"}))

        assert [call.arguments for call in client.calls] == [{"id": "ISSUE-42"}]


def test_a_resumed_call_is_held_to_the_limit_before_anything_else(tmp_path: Path) -> None:
    with active_repository(tmp_path / "a.db", _policies({"id": "ISSUE-[0-9]+"})) as repository:
        client = ConformanceToolClient()
        service = _service(repository, client)

        with pytest.raises(RequestValidationError, match="limited by the owner"):
            service.resume_with_input(_find("OTHER-1"), operation_id="operation-0", inputs={})

        assert client.calls == []


def test_without_a_limit_nothing_changes(tmp_path: Path) -> None:
    with active_repository(tmp_path / "a.db", _policies(None)) as repository:
        client = ConformanceToolClient()
        service = _service(repository, client)

        assert service.execute(_find("anything")).operation_status == COMPLETED


# --- publishing ------------------------------------------------------------------------------


def _tool_policy(patterns: object) -> tuple[dict[str, object], dict[str, object]]:
    primitive: dict[str, object] = {
        "type": "tool",
        "name": "find_issue",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
    }
    policy: dict[str, object] = {
        "primitiveKey": "tool:find_issue",
        "allowed": True,
        "publicOperationId": "find_issue",
        "classification": "write",
        "maxResultBytes": 1000,
        "argumentPatterns": patterns,
    }
    return primitive, policy


def test_a_limit_must_be_a_safe_pattern_on_an_argument_the_tool_has() -> None:
    assert validate_exposure_policy(*_tool_policy({"id": "ISSUE-[0-9]+"})) == []

    unknown = validate_exposure_policy(*_tool_policy({"project": "ENG"}))
    assert any("names an argument the tool lacks" in issue for issue in unknown)

    runaway = validate_exposure_policy(*_tool_policy({"id": "(a+)+$"}))
    assert any("catastrophic backtracking" in issue for issue in runaway)

    broken = validate_exposure_policy(*_tool_policy({"id": "["}))
    assert broken

    not_a_map = validate_exposure_policy(*_tool_policy(["id"]))
    assert not_a_map

    crowded = validate_exposure_policy(*_tool_policy({f"a{n}": "x" for n in range(11)}))
    assert any("more than 10" in issue for issue in crowded)


# --- what Muse reads -------------------------------------------------------------------------


def test_the_spec_tells_the_caller_the_limit_and_makes_the_argument_required() -> None:
    from muse_mcp_adapter.control_plane.compile_catalog import compile_catalog

    primitive, policy = _tool_policy({"id": values_pattern(["ENG", "OPS"])})
    policy["classification"] = "read"
    policy["readOnlyReviewed"] = True
    catalog = compile_catalog(
        catalog_version="1.0.0",
        server={"id": "project-system", "endpoint": "https://mcp.example.test/mcp"},
        protocol_version="2025-11-25",
        discovered_at=_NOW,
        published_at=_NOW,
        primitives=[primitive],
        policies=[policy],
    )

    document = compile_openapi(catalog, public_url="https://a.example.com", slug="abc")
    validate(dict(document))
    paths: Any = document["paths"]
    operation: Any = paths["/operations/find_issue"]["post"]
    schema: Any = operation["requestBody"]["content"]["application/json"]["schema"]

    assert "Limits set by the owner: id must be one of ENG, OPS." in operation["description"]
    assert schema["required"] == ["id"]
    assert schema["properties"]["id"]["pattern"] == "^(?:(?:ENG|OPS))$"
    assert "The owner limits this to one of ENG, OPS." in schema["properties"]["id"]["description"]
