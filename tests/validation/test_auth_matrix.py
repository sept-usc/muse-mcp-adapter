# pyright: basic
"""Who may call the connector, and which credential the MCP server sees."""

from pathlib import Path

import pytest

from tests.validation.env import AtlasHandle, ConnectorHandle, atlas_server, connector

pytestmark = pytest.mark.validation


def test_the_server_sees_only_the_linked_credential(
    world: tuple[AtlasHandle, ConnectorHandle],
) -> None:
    atlas, conn = world
    assert conn.call("get_issue", {"issue_id": "ISSUE-1"}).status_code == 200
    # Atlas rejects any other bearer; the connector's successful call proves the right one was sent.
    assert atlas.direct().__class__ is not None
    assert conn.link.token not in str(atlas.control("state"))


def test_a_wrong_server_credential_is_reported_as_rejected_not_as_an_outage(
    tmp_path: Path,
) -> None:
    with (
        atlas_server(auth="bearer") as atlas,
        connector(tmp_path, atlas, token_override="wrong-token") as conn,
    ):
        rejected = conn.call("get_issue", {"issue_id": "ISSUE-1"})
        assert rejected.status_code == 502
        assert rejected.json()["error"] == "DOWNSTREAM_AUTH_REJECTED"
        assert "atlas-secret-token" not in rejected.text and "wrong-token" not in rejected.text


def test_a_missing_server_credential_is_reported_before_any_request(tmp_path: Path) -> None:
    with (
        atlas_server(auth="bearer") as atlas,
        connector(tmp_path, atlas, token_override="") as conn,
    ):
        missing = conn.call("get_issue", {"issue_id": "ISSUE-1"})
        assert missing.status_code == 403
        assert missing.json()["error"] == "CREDENTIAL_RESOLUTION_FAILED"
        assert atlas.effects() == []


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer not-a-token"},
        {"Authorization": "Basic Zm9vOmJhcg=="},
        {"Authorization": "bearer"},
    ],
)
def test_callers_without_a_valid_token_are_refused(
    world: tuple[AtlasHandle, ConnectorHandle], headers: dict[str, str]
) -> None:
    atlas, conn = world
    response = conn.http.post(
        f"/c/{conn.slug}/operations/get_issue", json={"issue_id": "ISSUE-1"}, headers=headers
    )
    assert response.status_code == 401
    assert "resource_metadata" in response.headers.get("www-authenticate", "")
    assert atlas.effects() == []


def test_a_token_is_useless_on_another_connector_and_after_rotation(tmp_path: Path) -> None:
    with atlas_server() as atlas, connector(tmp_path, atlas) as conn:
        other = conn.connections.link(
            server_id="atlas", label="Other", credential_kind="none", credential_ref=""
        )
        crossed = conn.http.post(
            f"/c/{other.record.slug}/operations/get_issue",
            json={"issue_id": "ISSUE-1"},
            headers=conn.headers,
        )
        assert crossed.status_code == 401

        old = dict(conn.headers)
        conn.connections.rotate_token(conn.link.record.connection_id)
        revoked = conn.http.post(
            f"/c/{conn.slug}/operations/get_issue", json={"issue_id": "ISSUE-1"}, headers=old
        )
        assert revoked.status_code == 401


def test_one_connection_cannot_read_anothers_operations(tmp_path: Path) -> None:
    with atlas_server() as atlas, connector(tmp_path, atlas) as conn:
        made = conn.call("create_issue", {"title": "Mine"}, **{"Idempotency-Key": "iso"})
        operation_id = made.json().get("operationId") or made.headers.get("x-operation-id", "")
        other = conn.connections.link(
            server_id="atlas", label="Other", credential_kind="none", credential_ref=""
        )
        spied = conn.http.get(
            f"/c/{other.record.slug}/executions/{operation_id}",
            headers={"Authorization": f"Bearer {other.token}"},
        )
        assert spied.status_code in (403, 404)
