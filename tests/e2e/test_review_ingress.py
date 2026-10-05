"""End-to-end checks of the Muse-facing HTTP ingress and its mandatory controls."""

import json
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from muse_mcp_adapter.application.bootstrap import build_http_application
from muse_mcp_adapter.application.runtime import Principal
from muse_mcp_adapter.application.serve import (
    build_application,
    load_serve_config,
    open_repository,
)
from muse_mcp_adapter.credentials.resolver import InMemoryCredentialResolver
from muse_mcp_adapter.data_plane.approval import HmacApprovalVerifier
from muse_mcp_adapter.domain.errors import InvalidStateError
from muse_mcp_adapter.domain.models import ApprovalEvidence, OperationRecord
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from tests.fixtures.active_runtime import active_repository
from tests.fixtures.read_runtime import ConformanceReadClient
from tests.fixtures.tool_runtime import ConformanceToolClient

_NOW = "2026-10-04T18:05:00Z"
_APPROVALS = HmacApprovalVerifier(b"ingress-test-approval-secret-32-bytes!")


class _Downstream(ConformanceToolClient, ConformanceReadClient):
    def __init__(self) -> None:
        ConformanceToolClient.__init__(self)
        ConformanceReadClient.__init__(self)


def _credentials() -> InMemoryCredentialResolver:
    resolver = InMemoryCredentialResolver()
    for identity in ("user-1", "user-2"):
        resolver.register(
            local_identity_id=identity,
            connection_id="connection-1",
            server_id="project-system",
            credential_ref=f"secret://project-system/{identity}",
            access_token="token",
            scopes=frozenset({"issues:read", "issues:write", "workspace:read"}),
        )
    return resolver


@contextmanager
def _client(
    tmp_path: Path, *, rate_limit: int = 100, downstream: _Downstream | None = None
) -> Generator[tuple[TestClient, SqliteControlPlaneRepository, _Downstream]]:
    downstream = downstream or _Downstream()
    with active_repository(tmp_path / "adapter.db") as repository:
        app = build_http_application(
            repository=repository,
            credential_resolver=_credentials(),
            mcp_client=downstream,
            approval_verifier=_APPROVALS,
            ingress_tokens={
                "token-user-1": Principal(
                    "user-1", frozenset({"connection-1"}), frozenset({"project-system"})
                ),
                "token-user-2": Principal(
                    "user-2", frozenset({"connection-1"}), frozenset({"project-system"})
                ),
                "token-other": Principal(
                    "user-1", frozenset({"connection-1"}), frozenset({"another-server"})
                ),
                "token-admin": Principal(
                    "admin", frozenset({"connection-1"}), frozenset({"project-system"}), True
                ),
            },
            artifact_root=tmp_path / "artifacts",
            clock=lambda: _NOW,
            rate_limit=rate_limit,
        )
        with TestClient(app) as client:
            yield client, repository, downstream


def _evidence_json(evidence: ApprovalEvidence) -> dict[str, str]:
    return {
        "approvalId": evidence.approval_id,
        "classification": evidence.classification,
        "actionDigest": evidence.action_digest,
        "approvedAt": evidence.approved_at,
        "expiresAt": evidence.expires_at,
        "signature": evidence.signature,
    }


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _body(operation_arguments: dict[str, object], **extra: object) -> dict[str, object]:
    return {
        "serverId": "project-system",
        "connectionId": "connection-1",
        "arguments": operation_arguments,
        **extra,
    }


def test_authentication_is_mandatory_and_status_codes_are_meaningful(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _, _):
        url = "/v1/operations/find_issue"
        assert client.post(url, json=_body({"id": "ISSUE-42"})).status_code == 401
        assert (
            client.post(url, json=_body({"id": "ISSUE-42"}), headers=_auth("wrong")).status_code
            == 401
        )
        assert client.get("/health").status_code == 200

        ok = client.post(url, json=_body({"id": "ISSUE-42"}), headers=_auth("token-user-1"))
        assert ok.status_code == 200
        assert ok.json()["structuredContent"]["id"] == "ISSUE-42"

        # validation problems are 400, not 403; unknown operations are authorization failures
        invalid = client.post(url, json=_body({}), headers=_auth("token-user-1"))
        assert invalid.status_code == 400
        not_json = client.post(url, content=b"{nope", headers=_auth("token-user-1"))
        assert not_json.status_code == 400
        missing_args = client.post(
            url,
            json={"serverId": "project-system", "connectionId": "connection-1"},
            headers=_auth("token-user-1"),
        )
        assert missing_args.status_code == 400
        unknown = client.post(
            "/v1/operations/delete_everything", json=_body({}), headers=_auth("token-user-1")
        )
        assert unknown.status_code == 403
        too_big = client.post(
            url,
            content=b'{"serverId":"' + b"x" * (1024 * 1024 + 10) + b'"}',
            headers=_auth("token-user-1"),
        )
        assert too_big.status_code == 413


def test_caller_cannot_choose_identity_server_or_connection(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _, _):
        url = "/v1/operations/find_issue"
        wrong_server = client.post(url, json=_body({"id": "x"}), headers=_auth("token-other"))
        assert wrong_server.status_code == 403
        wrong_connection = client.post(
            url,
            json={**_body({"id": "x"}), "connectionId": "someone-elses"},
            headers=_auth("token-user-1"),
        )
        assert wrong_connection.status_code == 403
        # An attempted identity override in the body is ignored: it is not part of the contract.
        spoof = client.post(
            url,
            json={**_body({"id": "ISSUE-42"}), "localIdentityId": "user-2"},
            headers=_auth("token-user-1"),
        )
        assert spoof.status_code == 200


def test_writes_require_signed_approval_over_http(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, repository, downstream):
        url = "/v1/operations/create_issue"
        headers = _auth("token-user-1")
        body = _body({"title": "Example"}, idempotencyKey="key-1")

        first = client.post(url, json=body, headers=headers)
        assert first.status_code == 409
        assert first.json()["error"] == "APPROVAL_REQUIRED"
        digest = first.json()["actionDigest"]

        forged = HmacApprovalVerifier(b"attacker-secret-attacker-secret-attacker!").issue(
            approval_id="approval-1",
            classification="write",
            action_digest=digest,
            approved_at="2026-10-04T18:00:00Z",
            expires_at="2026-10-04T18:10:00Z",
        )

        rejected = client.post(
            url, json={**body, "approval": _evidence_json(forged)}, headers=headers
        )
        assert rejected.status_code == 409
        assert "signature" in rejected.json()["message"]
        assert downstream.calls == []

        genuine = _APPROVALS.issue(
            approval_id="approval-2",
            classification="write",
            action_digest=digest,
            approved_at="2026-10-04T18:00:00Z",
            expires_at="2026-10-04T18:10:00Z",
        )
        done = client.post(url, json={**body, "approval": _evidence_json(genuine)}, headers=headers)
        assert done.status_code == 200
        assert len(downstream.calls) == 1

        # The same request is idempotent and never dispatches twice.
        again = client.post(
            url, json={**body, "approval": _evidence_json(genuine)}, headers=headers
        )
        assert again.status_code == 200
        assert len(downstream.calls) == 1

        audit = repository.list_runtime_audit()
        assert [row["outcome"] for row in audit][-3:] == ["failed", "completed", "completed"]


def test_operations_are_isolated_between_identities(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, repository, _):
        client.post(
            "/v1/operations/find_issue",
            json=_body({"id": "ISSUE-42"}),
            headers=_auth("token-user-1"),
        )
        row = repository._connection.execute(  # pyright: ignore[reportPrivateUsage]
            "SELECT operation_id FROM operations LIMIT 1"
        ).fetchone()
        path = f"/v1/executions/{row['operation_id']}"
        assert client.get(path, headers=_auth("token-user-1")).status_code == 200
        assert client.get(path, headers=_auth("token-user-2")).status_code == 403
        assert client.get(path, headers=_auth("token-admin")).status_code == 200


def test_rate_limit_returns_429_and_every_request_is_audited(tmp_path: Path) -> None:
    with _client(tmp_path, rate_limit=2) as (client, repository, _):
        url = "/v1/operations/find_issue"
        statuses = [
            client.post(
                url, json=_body({"id": "ISSUE-42"}), headers=_auth("token-user-1")
            ).status_code
            for _ in range(3)
        ]
        assert statuses == [200, 200, 429]
        assert len(repository.list_runtime_audit()) == 2


def test_metrics_are_administrator_only_and_openapi_is_compiled_from_the_catalog(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as (client, _, _):
        client.post(
            "/v1/operations/find_issue",
            json=_body({"id": "ISSUE-42"}),
            headers=_auth("token-user-1"),
        )
        assert client.get("/metrics", headers=_auth("token-user-1")).status_code == 403
        assert (
            client.get("/metrics", headers=_auth("token-admin")).json()["requests.completed"] == 1
        )

        document = client.get(
            "/openapi.json?serverId=project-system", headers=_auth("token-user-1")
        )
        assert document.status_code == 200
        paths = document.json()["paths"]
        assert paths["/v1/operations/create_issue"]["post"]["x-muse-classification"] == "write"
        assert paths["/v1/operations/find_issue"]["post"]["x-muse-classification"] == "read"
        assert (
            client.get(
                "/openapi.json?serverId=another-server", headers=_auth("token-user-1")
            ).status_code
            == 403
        )


def test_disabled_server_is_rejected_at_runtime(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, repository, _):
        repository.set_server_enabled("project-system", False, _NOW)
        response = client.post(
            "/v1/operations/find_issue",
            json=_body({"id": "ISSUE-42"}),
            headers=_auth("token-user-1"),
        )
        assert response.status_code == 403
        assert response.json()["error"] == "SERVER_DISABLED"


# --- serve configuration: secrets only ever come from the environment ---------------------


def _config(path: Path, **overrides: object) -> Path:
    config: dict[str, object] = {
        "artifactRoot": str(path.parent / "artifacts"),
        "approvalSecretEnv": "TEST_APPROVAL_SECRET",
        "ingress": [
            {
                "tokenEnv": "TEST_TOKEN_A",
                "identity": "user-1",
                "servers": ["project-system"],
                "connections": ["connection-1"],
            }
        ],
        "credentials": [
            {
                "identity": "user-1",
                "connection": "connection-1",
                "server": "project-system",
                "ref": "secret://project-system/user-1",
                "tokenEnv": "TEST_DOWNSTREAM_TOKEN",
                "scopes": ["issues:read"],
            }
        ],
    }
    config.update(overrides)
    path.write_text(json.dumps(config))
    return path


_ENV = {
    "TEST_APPROVAL_SECRET": "serve-approval-secret-serve-approval-secret",
    "TEST_TOKEN_A": "ingress-token-a",
    "TEST_DOWNSTREAM_TOKEN": "downstream-token",
}


def test_serve_configuration_builds_a_working_authenticated_app(tmp_path: Path) -> None:
    config = load_serve_config(_config(tmp_path / "serve.json"))
    with active_repository(tmp_path / "adapter.db") as repository:
        app = build_application(
            repository, config, _ENV, mcp_client=_Downstream(), clock=lambda: _NOW
        )
        with TestClient(app) as client:
            assert client.get("/openapi.json?serverId=project-system").status_code == 401
            ok = client.post(
                "/v1/operations/find_issue",
                json=_body({"id": "ISSUE-42"}),
                headers=_auth("ingress-token-a"),
            )
            assert ok.status_code == 200


def test_serve_configuration_rejects_missing_secrets_and_duplicate_tokens(tmp_path: Path) -> None:
    config = load_serve_config(_config(tmp_path / "serve.json"))
    without_token = {k: v for k, v in _ENV.items() if k != "TEST_TOKEN_A"}
    with (
        active_repository(tmp_path / "adapter.db") as repository,
        pytest.raises(InvalidStateError, match="TEST_TOKEN_A"),
    ):
        build_application(repository, config, without_token)

    duplicate = _config(
        tmp_path / "dup.json",
        ingress=[
            {"tokenEnv": "TEST_TOKEN_A", "identity": "a", "servers": ["s"], "connections": ["c"]},
            {"tokenEnv": "TEST_TOKEN_A", "identity": "b", "servers": ["s"], "connections": ["c"]},
        ],
    )
    with (
        active_repository(tmp_path / "adapter2.db") as repository,
        pytest.raises(InvalidStateError, match="distinct"),
    ):
        build_application(repository, load_serve_config(duplicate), _ENV)

    leaked = tmp_path / "leaky.json"
    leaked.write_text(json.dumps({"artifactRoot": "x", "approvalSecretEnv": "A", "ingress": []}))
    with pytest.raises(InvalidStateError, match="caller"):
        load_serve_config(leaked)


def test_integrity_key_is_applied_when_configured(tmp_path: Path) -> None:
    config = load_serve_config(_config(tmp_path / "serve.json", integrityKeyEnv="TEST_INTEGRITY"))
    with pytest.raises(InvalidStateError, match="TEST_INTEGRITY"):
        open_repository(tmp_path / "x.db", config, _ENV)
    env = {**_ENV, "TEST_INTEGRITY": "integrity-key-integrity-key-integrity"}
    with open_repository(tmp_path / "x.db", config, env) as repository:
        repository.initialize()


def _seed_operation(
    repository: SqliteControlPlaneRepository, operation_id: str, classification: str, status: str
) -> None:
    repository.create_operation(
        OperationRecord(
            operation_id=operation_id,
            request_id=f"request-{operation_id}",
            server_id="project-system",
            catalog_version="1.0.0",
            public_operation_id="create_issue",
            local_identity_id="user-1",
            connection_id="connection-1",
            classification=classification,
            status=status,
            idempotency_key=f"key-{operation_id}",
            request_digest="sha256:abc",
            approval_id=None,
            created_at=_NOW,
            updated_at=_NOW,
            result=None,
            error_code=None,
            error_message=None,
            retryable=False,
        )
    )


def test_cancelling_unstarted_work_is_applied_but_a_running_write_is_only_requested(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as (client, repository, _):
        _seed_operation(repository, "waiting", "write", "APPROVAL_REQUIRED")
        _seed_operation(repository, "in-flight", "write", "RUNNING")
        headers = _auth("token-user-1")

        applied = client.post("/v1/executions/waiting/cancel", json={}, headers=headers)
        assert applied.status_code == 200
        assert applied.json()["cancellation"] == "applied"
        assert applied.json()["status"] == "CANCELLED"

        requested = client.post(
            "/v1/executions/in-flight/cancel", json={"reason": "changed my mind"}, headers=headers
        )
        assert requested.status_code == 202
        assert requested.json()["cancellation"] == "requested"
        assert requested.json()["status"] == "RUNNING"  # never reported as cancelled
        assert "final outcome" in requested.json()["message"]

        events = [e.event_type for e in repository.list_operation_events("in-flight")]
        assert "cancel_requested" in events
        assert repository.get_operation("in-flight").status == "RUNNING"

        # another identity cannot cancel someone else's operation
        denied = client.post(
            "/v1/executions/in-flight/cancel", json={}, headers=_auth("token-user-2")
        )
        assert denied.status_code == 403
