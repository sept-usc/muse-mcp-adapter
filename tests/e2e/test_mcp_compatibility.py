"""What the connector shows Muse versus what the MCP server offers.

A sample MCP server covering the MCP surface runs on a real socket behind the real adapter.
Every primitive must survive the trip: schemas, errors, progress, questions, resources, prompts.
"""

import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from openapi_spec_validator import validate
from starlette.testclient import TestClient

from muse_mcp_adapter.application.control_plane_service import ControlPlaneService
from muse_mcp_adapter.application.policy_template import draft_policies
from muse_mcp_adapter.application.serve import ServeConfig, build_application
from muse_mcp_adapter.connections import ConnectionService
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from muse_mcp_adapter.transport.sdk_client import McpSdkClient, StreamableHttpSessionFactory
from muse_mcp_adapter.transport.security import EndpointSecurityPolicy
from tests.fixtures.live_server import run_asgi

sys.path.insert(0, str(Path(__file__).parents[2] / "examples"))
from sample_mcp_server import build_server  # noqa: E402  (the example is not a package)

_LOCAL = EndpointSecurityPolicy.local_development()


@dataclass
class Connector:
    http: TestClient
    repository: SqliteControlPlaneRepository
    slug: str
    headers: dict[str, str]
    spec: dict[str, Any]

    def call(self, operation: str, body: dict[str, Any] | None = None, **headers: str) -> Any:
        return self.http.post(
            f"/c/{self.slug}/operations/{operation}",
            json=body if body is not None else None,
            headers={**self.headers, **headers},
        )


@contextmanager
def _connector(tmp_path: Path) -> Generator[Connector]:
    database = tmp_path / "adapter.db"
    with run_asgi(build_server().streamable_http_app()) as port:
        endpoint = f"http://127.0.0.1:{port}/mcp"
        with SqliteControlPlaneRepository(database) as repository:
            repository.initialize()
            control = ControlPlaneService(repository, endpoint_policy=_LOCAL)
            control.register_server(
                server_id="sample", display_name="Sample", endpoint=endpoint, auth_profile="none"
            )
            snapshot = control.discover(
                "sample", McpSdkClient(StreamableHttpSessionFactory(endpoint_policy=_LOCAL))
            )
            policies = draft_policies(snapshot, expose_reads=True)
            for policy in policies:
                policy["allowed"] = True  # the owner reviewed the draft and switched it all on
            control.publish_catalog(server_id="sample", catalog_version="1.0.0", policies=policies)
            control.activate_catalog("sample", "1.0.0")

            linked = ConnectionService(repository).link(
                server_id="sample", label="Muse", credential_kind="none"
            )
            config = ServeConfig(
                public_url="http://127.0.0.1:8123",
                allow_insecure_http=True,
                allow_private_networks=True,
            )
            app = build_application(repository, config, {}, database=database)
            with TestClient(app) as http:
                slug = linked.record.slug
                spec = http.get(f"/c/{slug}/openapi.json").json()
                yield Connector(
                    http, repository, slug, {"Authorization": f"Bearer {linked.token}"}, spec
                )


def _operations(connector: Connector) -> dict[str, dict[str, Any]]:
    return {
        path.rsplit("/", 1)[-1]: item["post"]
        for path, item in connector.spec["paths"].items()
        if path.startswith("/operations/")
    }


def _body_schema(operation: dict[str, Any]) -> dict[str, Any]:
    return operation["requestBody"]["content"]["application/json"]["schema"]


def test_every_server_primitive_becomes_an_operation_in_a_valid_spec(tmp_path: Path) -> None:
    with _connector(tmp_path) as c:
        validate(c.spec)
        assert set(_operations(c)) == {
            "find_issue", "create_issue", "search_issues", "assign_issue", "break_things",
            "reindex", "schedule_review", "export_all", "workspace_guide", "issue_document",
            "triage_issue",
        }  # fmt: skip
        operations = _operations(c)
        assert operations["find_issue"]["x-muse-classification"] == "read"
        assert operations["create_issue"]["x-requires-confirmation"] is True
        assert "requestBody" not in operations["workspace_guide"]  # a fixed resource takes nothing


def test_schema_shapes_reach_muse_intact(tmp_path: Path) -> None:
    with _connector(tmp_path) as c:
        operations = _operations(c)

        search = _body_schema(operations["search_issues"])
        assert search["required"] == ["query"]  # optional parameters stay optional
        assert search["properties"]["status"]["enum"] == ["open", "closed"]
        assert search["properties"]["labels"]["anyOf"][0]["type"] == "array"
        assert search["properties"]["limit"]["default"] == 10

        assign = _body_schema(operations["assign_issue"])
        assert "$defs" not in assign
        reference = assign["properties"]["assignee"]["$ref"]
        assert reference.startswith("#/components/schemas/assign_issue__")
        target = c.spec["components"]["schemas"][reference.rsplit("/", 1)[-1]]
        assert target["required"] == ["name", "email"]

        template = _body_schema(operations["issue_document"])
        assert template["required"] == ["issue_id"]
        prompt = _body_schema(operations["triage_issue"])
        assert prompt["required"] == ["issue_id"] and "focus" in prompt["properties"]


def test_tools_return_structured_results_and_enforce_their_schemas(tmp_path: Path) -> None:
    with _connector(tmp_path) as c:
        found = c.call("find_issue", {"issue_id": "ISSUE-1"})
        assert found.status_code == 200
        assert found.json()["structuredContent"] == {
            "id": "ISSUE-1",
            "title": "Login fails on Safari",
        }

        searched = c.call(
            "search_issues", {"query": "export", "status": "closed", "labels": ["perf"], "limit": 5}
        ).json()["structuredContent"]
        assert searched["status"] == "closed" and searched["labels"] == ["perf"]
        assert [item["id"] for item in searched["results"]] == ["ISSUE-2"]

        bad_enum = c.call("search_issues", {"query": "x", "status": "pending"})
        assert bad_enum.status_code == 400 and "status" in bad_enum.json()["message"]
        assert c.call("search_issues", {}).status_code == 400  # a required field is missing

        nested = c.call(
            "assign_issue",
            {
                "issue_id": "ISSUE-1",
                "assignee": {"name": "Ada", "email": "ada@example.com"},
                "watchers": [{"name": "Grace", "email": "grace@example.com"}],
            },
        )
        assert nested.status_code == 200
        assert "assigned to Ada with 1 watchers" in nested.json()["contents"][0]["text"]
        incomplete = c.call("assign_issue", {"issue_id": "ISSUE-1", "assignee": {"name": "Ada"}})
        assert incomplete.status_code == 400


def test_a_tool_error_is_reported_as_an_error_not_a_success(tmp_path: Path) -> None:
    with _connector(tmp_path) as c:
        failed = c.call("break_things", {})
        assert failed.status_code == 502
        assert failed.json()["error"] == "DOWNSTREAM_TOOL_ERROR"
        assert "database is on fire" in failed.json()["message"]

        row = c.repository.run(
            lambda db: db.execute(
                "SELECT status, retryable FROM operations WHERE public_operation_id = ?",
                ("break_things",),
            ).fetchone()
        )
        assert (row["status"], row["retryable"]) == ("FAILED", 0)  # not retried blindly


def test_progress_notifications_are_recorded_on_the_operation(tmp_path: Path) -> None:
    with _connector(tmp_path) as c:
        done = c.call("reindex", {"steps": 3})
        assert done.status_code == 200
        events = c.repository.list_operation_events(done.json()["operationId"])
        progress = [event.data for event in events if event.event_type == "progress"]
        assert [item["progress"] for item in progress] == [1, 2, 3]
        assert progress[0]["message"] == "Indexed batch 1"


def test_a_question_from_the_tool_reaches_the_user_and_the_answer_resumes_it(
    tmp_path: Path,
) -> None:
    with _connector(tmp_path) as c:
        asked = c.call("schedule_review", {"issue_id": "ISSUE-1"})
        assert asked.status_code == 409
        body = asked.json()
        assert body["error"] == "INPUT_REQUIRED"
        assert "reviewed" in body["message"]
        assert "time" in body["schema"]["properties"]  # what Muse must ask the user for

        status = c.http.get(
            f"/c/{c.slug}/executions/{body['operationId']}", headers=c.headers
        ).json()
        assert status["status"] == "INPUT_REQUIRED"

        url = f"/c/{c.slug}/executions/{body['operationId']}/resume"
        wrong = c.http.post(
            url,
            json={"arguments": {"issue_id": "ISSUE-1"}, "inputs": {"time": 5}},
            headers=c.headers,
        )
        assert wrong.status_code == 400
        answered = c.http.post(
            url,
            json={"arguments": {"issue_id": "ISSUE-1"}, "inputs": {"time": "Tuesday 10am"}},
            headers=c.headers,
        )
        assert answered.status_code == 200, answered.text
        assert "scheduled for Tuesday 10am" in answered.json()["contents"][0]["text"]


def test_results_beyond_the_published_limit_are_refused(tmp_path: Path) -> None:
    with _connector(tmp_path) as c:
        response = c.call("export_all", {})
        assert response.status_code == 502
        assert response.json()["error"] == "DOWNSTREAM_CONTENT_REJECTED"


def test_resources_and_templates_are_read_through_their_own_operations(tmp_path: Path) -> None:
    with _connector(tmp_path) as c:
        guide = c.call("workspace_guide")
        assert guide.status_code == 200
        assert guide.json()["contents"][0]["text"] == "Issues are triaged every Monday."
        assert guide.json()["contents"][0]["uri"] == "mcp://workspace/guide"

        document = c.call("issue_document", {"issue_id": "ISSUE-2"})
        assert document.json()["contents"][0]["text"] == "ISSUE-2: Export is slow"

        assert c.call("issue_document", {"issue_id": ".."}).status_code == 400
        for hostile in ("a/../../b", "x\x00y", "../etc/passwd"):
            assert c.call("issue_document", {"issue_id": hostile}).status_code < 500


def test_prompts_keep_their_roles_and_arguments(tmp_path: Path) -> None:
    with _connector(tmp_path) as c:
        prompt = c.call("triage_issue", {"issue_id": "ISSUE-1", "focus": "revenue"})
        assert prompt.status_code == 200, prompt.text
        message = prompt.json()["messages"][0]
        assert message["role"] == "user"
        assert "focusing on revenue" in message["content"][0]["text"]
        assert c.call("triage_issue", {}).status_code == 400


def test_a_write_retried_with_the_same_key_runs_once_on_the_real_server(tmp_path: Path) -> None:
    with _connector(tmp_path) as c:
        first = c.call("create_issue", {"title": "Dark mode"}, **{"Idempotency-Key": "retry-1"})
        second = c.call("create_issue", {"title": "Dark mode"}, **{"Idempotency-Key": "retry-1"})
        assert first.json()["operationId"] == second.json()["operationId"]
        created = first.json()["structuredContent"]["id"]
        assert c.call("find_issue", {"issue_id": created}).json()["structuredContent"]["title"] == (
            "Dark mode"
        )
        search = c.call("search_issues", {"query": "dark mode"}).json()["structuredContent"]
        assert len(search["results"]) == 1  # it was created exactly once


@pytest.mark.parametrize("operation", ["find_issue", "workspace_guide"])
def test_the_spec_is_public_but_calls_are_not(tmp_path: Path, operation: str) -> None:
    with _connector(tmp_path) as c:
        assert c.http.get(f"/c/{c.slug}/openapi.json").status_code == 200
        assert c.http.post(f"/c/{c.slug}/operations/{operation}", json={}).status_code == 401
