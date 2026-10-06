"""The Muse-facing HTTP service: per-connection tokens, public spec, writes and limits."""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openapi_spec_validator import validate
from starlette.testclient import TestClient

from muse_mcp_adapter.application.bootstrap import build_http_application
from muse_mcp_adapter.application.oauth_web import OAuthWebFlow
from muse_mcp_adapter.connections import (
    ConnectionCredentialResolver,
    ConnectionService,
    LinkedConnection,
)
from muse_mcp_adapter.data_plane.tool_client import McpReauthorizationRequired
from muse_mcp_adapter.domain.models import McpToolResult, OperationRecord
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from muse_mcp_adapter.transport.oauth import InMemoryOAuthStorageBackend
from muse_mcp_adapter.transport.security import EndpointSecurityPolicy
from tests.fixtures.active_runtime import active_repository
from tests.fixtures.read_runtime import ConformanceReadClient
from tests.fixtures.tool_runtime import ConformanceToolClient

_NOW = "2026-10-04T18:05:00Z"


class _Downstream(ConformanceToolClient, ConformanceReadClient):
    def __init__(self) -> None:
        ConformanceToolClient.__init__(self)
        ConformanceReadClient.__init__(self)
        self.reauthorize = False

    def call_tool(self, **kwargs: Any) -> McpToolResult:
        if self.reauthorize:
            raise McpReauthorizationRequired("The account must be authorized again.")
        return super().call_tool(**kwargs)


@dataclass
class Harness:
    http: TestClient
    repository: SqliteControlPlaneRepository
    connections: ConnectionService
    downstream: _Downstream

    def link(self, label: str = "phone") -> LinkedConnection:
        return self.connections.link(
            server_id="project-system", label=label, credential_kind="none"
        )

    @staticmethod
    def auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}


@contextmanager
def _harness(
    tmp_path: Path, *, rate_limit: int = 100, with_oauth_flow: bool = False
) -> Generator[Harness]:
    downstream = _Downstream()
    with active_repository(tmp_path / "adapter.db") as repository:
        flow = (
            OAuthWebFlow(
                repository=repository,
                public_url="https://adapter.example.com",
                backend=InMemoryOAuthStorageBackend(),
                endpoint_policy=EndpointSecurityPolicy(),
                clock=lambda: "2026-10-04T18:05:00Z",
            )
            if with_oauth_flow
            else None
        )
        app = build_http_application(
            repository=repository,
            credential_resolver=ConnectionCredentialResolver(repository, {}),
            mcp_client=downstream,
            artifact_root=tmp_path / "artifacts",
            public_url="https://adapter.example.com",
            clock=lambda: _NOW,
            oauth_flow=flow,
            rate_limit=rate_limit,
        )
        with TestClient(app) as http:
            yield Harness(http, repository, ConnectionService(repository), downstream)


def test_spec_is_public_valid_and_asks_muse_only_for_what_it_can_supply(tmp_path: Path) -> None:
    with _harness(tmp_path) as h:
        linked = h.link()
        response = h.http.get(f"/c/{linked.record.slug}/openapi.json")  # no token needed
        assert response.status_code == 200
        document = response.json()
        validate(document)

        assert document["servers"] == [
            {"url": f"https://adapter.example.com/c/{linked.record.slug}"}
        ]
        assert document["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
        text = response.text
        assert "serverId" not in text and "connectionId" not in text

        create = document["paths"]["/operations/create_issue"]["post"]
        find = document["paths"]["/operations/find_issue"]["post"]
        assert create["x-requires-confirmation"] is True
        assert "x-requires-confirmation" not in find
        assert [p["name"] for p in create["parameters"]] == ["Idempotency-Key"]
        assert "parameters" not in find
        # the body is the tool's own input schema, not a wrapper
        schema = create["requestBody"]["content"]["application/json"]["schema"]
        assert "title" in schema["properties"]
        assert linked.token not in text


def test_unknown_or_revoked_connectors_look_the_same(tmp_path: Path) -> None:
    with _harness(tmp_path) as h:
        assert h.http.get("/c/not-a-slug/openapi.json").status_code == 404
        linked = h.link()
        h.connections.unlink(linked.record.connection_id)
        assert h.http.get(f"/c/{linked.record.slug}/openapi.json").status_code == 404


def test_authentication_is_mandatory_and_tied_to_the_connector_url(tmp_path: Path) -> None:
    with _harness(tmp_path) as h:
        first, second = h.link("first"), h.link("second")
        url = f"/c/{first.record.slug}/operations/find_issue"
        body = {"id": "ISSUE-42"}

        assert h.http.post(url, json=body).status_code == 401
        assert h.http.post(url, json=body, headers=h.auth("wrong")).status_code == 401
        ok = h.http.post(url, json=body, headers=h.auth(first.token))
        assert ok.status_code == 200
        assert ok.json()["structuredContent"]["id"] == "ISSUE-42"

        # a valid token for a different connector must not work, nor reveal that it exists
        crossed = h.http.post(url, json=body, headers=h.auth(second.token))
        assert crossed.status_code == 401
        wrong = h.http.post(url, json=body, headers=h.auth("mcpa_wrong"))
        assert crossed.json() == wrong.json()


def test_request_validation_and_error_statuses(tmp_path: Path) -> None:
    with _harness(tmp_path) as h:
        linked = h.link()
        base = f"/c/{linked.record.slug}/operations"
        headers = h.auth(linked.token)

        assert h.http.post(f"{base}/find_issue", json={}, headers=headers).status_code == 400
        assert (
            h.http.post(f"{base}/find_issue", content=b"{nope", headers=headers).status_code == 400
        )
        assert h.http.post(f"{base}/find_issue", json=[1], headers=headers).status_code == 400
        assert h.http.post(f"{base}/delete_all", json={}, headers=headers).status_code == 404
        big = b'{"id":"' + b"x" * (1024 * 1024 + 10) + b'"}'
        assert h.http.post(f"{base}/find_issue", content=big, headers=headers).status_code == 413
        bad_key = {**headers, "Idempotency-Key": "has space"}
        assert (
            h.http.post(f"{base}/create_issue", json={"title": "t"}, headers=bad_key).status_code
            == 400
        )


def test_writes_run_on_muses_confirmation_and_never_twice(tmp_path: Path) -> None:
    with _harness(tmp_path) as h:
        linked = h.link()
        url = f"/c/{linked.record.slug}/operations/create_issue"
        headers = h.auth(linked.token)

        # no approval evidence and no idempotency key: Muse confirmed, the adapter derives a key
        first = h.http.post(url, json={"title": "Login fails"}, headers=headers)
        assert first.status_code == 200, first.text
        repeat = h.http.post(url, json={"title": "Login fails"}, headers=headers)
        assert repeat.json()["operationId"] == first.json()["operationId"]
        assert len(h.downstream.calls) == 1

        # a different request is a different action
        other = h.http.post(url, json={"title": "Other"}, headers=headers)
        assert other.json()["operationId"] != first.json()["operationId"]

        # an explicit key controls deduplication exactly
        keyed = {**headers, "Idempotency-Key": "muse-retry-1"}
        a = h.http.post(url, json={"title": "Keyed"}, headers=keyed)
        b = h.http.post(url, json={"title": "Keyed"}, headers=keyed)
        assert a.json()["operationId"] == b.json()["operationId"]
        clash = h.http.post(url, json={"title": "Changed"}, headers=keyed)
        assert clash.status_code == 409  # same key, different request


def test_one_connection_cannot_see_anothers_operations(tmp_path: Path) -> None:
    with _harness(tmp_path) as h:
        first, second = h.link("first"), h.link("second")
        done = h.http.post(
            f"/c/{first.record.slug}/operations/find_issue",
            json={"id": "ISSUE-42"},
            headers=h.auth(first.token),
        )
        operation_id = done.json()["operationId"]

        mine = h.http.get(
            f"/c/{first.record.slug}/executions/{operation_id}", headers=h.auth(first.token)
        )
        assert mine.status_code == 200 and mine.json()["status"] == "COMPLETED"
        assert "result" not in mine.json()
        theirs = h.http.get(
            f"/c/{second.record.slug}/executions/{operation_id}", headers=h.auth(second.token)
        )
        assert theirs.status_code == 404


def test_rate_limit_is_per_connection_and_every_call_is_audited(tmp_path: Path) -> None:
    with _harness(tmp_path, rate_limit=2) as h:
        first, second = h.link("first"), h.link("second")

        def call(linked: LinkedConnection) -> int:
            return h.http.post(
                f"/c/{linked.record.slug}/operations/find_issue",
                json={"id": "x"},
                headers=h.auth(linked.token),
            ).status_code

        assert [call(first) for _ in range(3)] == [200, 200, 429]
        assert call(second) == 200  # another connection has its own allowance
        assert len(h.repository.list_runtime_audit()) == 3


def test_disabled_server_blocks_calls_and_hides_the_spec(tmp_path: Path) -> None:
    with _harness(tmp_path) as h:
        linked = h.link()
        h.repository.set_server_enabled("project-system", False, _NOW)
        call = h.http.post(
            f"/c/{linked.record.slug}/operations/find_issue",
            json={"id": "x"},
            headers=h.auth(linked.token),
        )
        assert call.status_code == 403
        assert h.http.get(f"/c/{linked.record.slug}/openapi.json").status_code == 404


def test_revoked_and_rotated_tokens_stop_working_immediately(tmp_path: Path) -> None:
    with _harness(tmp_path) as h:
        linked = h.link()
        url = f"/c/{linked.record.slug}/operations/find_issue"
        assert h.http.post(url, json={"id": "x"}, headers=h.auth(linked.token)).status_code == 200

        rotated = h.connections.rotate_token(linked.record.connection_id)
        assert h.http.post(url, json={"id": "x"}, headers=h.auth(linked.token)).status_code == 401
        assert h.http.post(url, json={"id": "x"}, headers=h.auth(rotated.token)).status_code == 200

        h.connections.unlink(linked.record.connection_id)
        assert h.http.post(url, json={"id": "x"}, headers=h.auth(rotated.token)).status_code == 401


def test_reauthorization_is_a_401_that_carries_a_one_time_link(tmp_path: Path) -> None:
    with _harness(tmp_path, with_oauth_flow=True) as h:
        linked = h.connections.link(
            server_id="project-system",
            label="o",
            credential_kind="oauth",
            scopes=["issues:read", "issues:write"],
        )
        h.downstream.reauthorize = True
        response = h.http.post(
            f"/c/{linked.record.slug}/operations/create_issue",
            json={"title": "t"},
            headers=h.auth(linked.token),
        )
        assert response.status_code == 401
        body = response.json()
        assert body["error"] == "REAUTHORIZATION_REQUIRED"
        assert body["authorizationUrl"].startswith("https://adapter.example.com/connect/")
        assert body["authorizationUrl"] in body["message"]
        # the write was never sent, so it is retryable and not stuck as UNKNOWN
        record = _only_operation(h.repository)
        assert record.status == "FAILED" and record.retryable


def _only_operation(repository: SqliteControlPlaneRepository) -> OperationRecord:
    row = repository._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "SELECT operation_id FROM operations"
    ).fetchone()
    return repository.get_operation(row["operation_id"])
