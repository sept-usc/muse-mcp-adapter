"""Regression tests for the security and policy findings of the implementation review."""

import asyncio
import os
import stat
from collections.abc import Mapping
from pathlib import Path

import pytest
from mcp.shared.auth import OAuthToken

from muse_mcp_adapter.control_plane.compile_catalog import compile_catalog
from muse_mcp_adapter.credentials.resolver import InMemoryCredentialResolver
from muse_mcp_adapter.data_plane.approval import HmacApprovalVerifier
from muse_mcp_adapter.domain.errors import (
    CatalogValidationError,
    InvalidStateError,
    UnsafeEndpointError,
)
from muse_mcp_adapter.domain.models import ApprovalEvidence, PublishedCatalog
from muse_mcp_adapter.operations.audit import _redact  # pyright: ignore[reportPrivateUsage]
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from muse_mcp_adapter.transport.oauth import (
    EncryptedFileOAuthStorageBackend,
    NamespacedTokenStorage,
)
from muse_mcp_adapter.transport.security import EndpointSecurityPolicy

_NOW = "2026-10-04T18:05:00Z"
_SERVER = {"id": "project-system", "endpoint": "https://mcp.example.test/mcp"}


def _tool(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "type": "tool",
        "name": "create_issue",
        "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}},
    }
    value.update(overrides)
    return value


def _policy(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "primitiveKey": "tool:create_issue",
        "allowed": True,
        "publicOperationId": "create_issue",
        "classification": "write",
        "maxResultBytes": 1000,
    }
    value.update(overrides)
    return value


def _compile(primitive: Mapping[str, object], policy: Mapping[str, object]) -> PublishedCatalog:
    return compile_catalog(
        catalog_version="1.0.0",
        server=_SERVER,
        protocol_version="2026-07-28",
        discovered_at=_NOW,
        published_at=_NOW,
        primitives=[primitive],
        policies=[policy],
    )


# --- Finding 2: optional policy fields get explicit defaults at publish time ---------------


def test_publish_fills_optional_policy_defaults() -> None:
    catalog = _compile(_tool(), _policy())

    policy = catalog.primitives[0].policy
    assert policy["requiredScopes"] == ()
    assert policy["timeoutSeconds"] == 30


@pytest.mark.parametrize("field", ["timeoutSeconds", "maxResultBytes"])
def test_boolean_values_are_not_accepted_as_integers(field: str) -> None:
    with pytest.raises(CatalogValidationError):
        _compile(_tool(), _policy(**{field: True}))


def test_payload_limits_have_an_upper_bound() -> None:
    with pytest.raises(CatalogValidationError, match="1 issue"):
        _compile(_tool(), _policy(maxResultBytes=10**12))


# --- Finding 8: URI scheme restrictions, argument patterns, schema regex safety ------------


def test_forbidden_resource_uri_schemes_cannot_be_published() -> None:
    primitive: dict[str, object] = {
        "type": "resource_template",
        "name": "files",
        "uriTemplate": "file:///etc/{name}",
    }
    policy = {
        "primitiveKey": "resource_template:files",
        "allowed": True,
        "publicOperationId": "read_files",
        "classification": "read",
        "resource": {"allowedMimeTypes": ["text/plain"], "maxBytes": 100},
    }
    with pytest.raises(CatalogValidationError) as raised:
        _compile(primitive, policy)
    assert any("scheme file" in issue for issue in raised.value.issues)


def test_resource_uri_must_match_the_allowed_scheme_list() -> None:
    primitive: dict[str, object] = {"type": "resource", "name": "guide", "uri": "https://a.test/x"}
    policy = {
        "primitiveKey": "resource:guide",
        "allowed": True,
        "publicOperationId": "read_guide",
        "classification": "read",
        "resource": {
            "allowedMimeTypes": ["text/plain"],
            "maxBytes": 100,
            "allowedUriSchemes": ["mcp"],
        },
    }
    with pytest.raises(CatalogValidationError):
        _compile(primitive, policy)


def test_catastrophic_schema_patterns_are_rejected_at_publish() -> None:
    schema = {
        "type": "object",
        "properties": {"title": {"type": "string", "pattern": "(a+)+$"}},
    }
    with pytest.raises(CatalogValidationError):
        _compile(_tool(inputSchema=schema), _policy())


def test_tool_without_read_only_hint_cannot_be_classified_read() -> None:
    with pytest.raises(CatalogValidationError, match="1 issue"):
        _compile(_tool(), _policy(classification="read"))

    reviewed = _compile(_tool(), _policy(classification="read", readOnlyReviewed=True))
    assert reviewed.primitives[0].classification == "read"

    hinted = _compile(_tool(annotations={"readOnlyHint": True}), _policy(classification="read"))
    assert hinted.primitives[0].classification == "read"


# --- Finding 3: SSRF guard ----------------------------------------------------------------


def _policy_with_dns(table: dict[str, list[str]]) -> EndpointSecurityPolicy:
    return EndpointSecurityPolicy(resolver=lambda host, _port: table[host])


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://2130706433/mcp",  # decimal 127.0.0.1
        "https://0x7f.1/mcp",  # hex/short form
        "https://127.1/mcp",
        "https://127.0.0.1/mcp",
        "https://[::1]/mcp",
        "https://[::ffff:7f00:1]/mcp",
        "https://169.254.169.254/latest",
        "https://localhost/mcp",
        "https://metadata.internal/mcp",
        "http://public.example/mcp",
        "https://user:pass@public.example/mcp",
    ],
)
def test_endpoint_policy_blocks_private_and_unsafe_forms(endpoint: str) -> None:
    policy = _policy_with_dns({"public.example": ["93.184.216.34"]})
    with pytest.raises(UnsafeEndpointError):
        policy.validate(endpoint)


def test_endpoint_policy_blocks_dns_names_that_resolve_privately() -> None:
    policy = _policy_with_dns({"rebind.example": ["10.0.0.5"], "mixed.example": ["8.8.8.8", "::1"]})
    with pytest.raises(UnsafeEndpointError):
        policy.validate("https://rebind.example/mcp")
    with pytest.raises(UnsafeEndpointError):
        policy.validate("https://mixed.example/mcp")
    # Registration-time validation is syntax-only and does not resolve.
    policy.validate("https://rebind.example/mcp", resolve=False)


def test_endpoint_policy_allows_public_hosts_and_honours_allowlist() -> None:
    policy = _policy_with_dns({"public.example": ["93.184.216.34"], "other.example": ["8.8.8.8"]})
    policy.validate("https://public.example/mcp")

    restricted = EndpointSecurityPolicy(
        allowed_hosts=frozenset({"public.example"}),
        resolver=lambda _host, _port: ["93.184.216.34"],
    )
    restricted.validate("https://public.example/mcp")
    with pytest.raises(UnsafeEndpointError, match="allowlist"):
        restricted.validate("https://other.example/mcp")


# --- Finding 1: approval evidence cannot be forged ----------------------------------------


_SECRET = b"review-regression-approval-secret-32+bytes"


def _issue(verifier: HmacApprovalVerifier, **overrides: str) -> ApprovalEvidence:
    fields = {
        "approval_id": "approval-1",
        "classification": "write",
        "action_digest": "sha256:abc",
        "approved_at": "2026-10-04T18:00:00Z",
        "expires_at": "2026-10-04T18:10:00Z",
    }
    fields.update(overrides)
    return verifier.issue(**fields)


def test_signed_approval_is_accepted_and_tampering_is_rejected() -> None:
    verifier = HmacApprovalVerifier(_SECRET)
    evidence = _issue(verifier)

    assert (
        verifier.verify(evidence, classification="write", action_digest="sha256:abc", now=_NOW)
        is None
    )

    forged = HmacApprovalVerifier(b"attacker-secret-attacker-secret-attacker!")
    forged_evidence = _issue(forged)
    assert "signature" in (
        verifier.verify(
            forged_evidence, classification="write", action_digest="sha256:abc", now=_NOW
        )
        or ""
    )

    unsigned = evidence.__class__(
        approval_id="approval-1",
        classification="write",
        action_digest="sha256:abc",
        approved_at="2026-10-04T18:00:00Z",
        expires_at="2999-01-01T00:00:00Z",
        signature=evidence.signature,
    )
    assert "signature" in (
        verifier.verify(unsigned, classification="write", action_digest="sha256:abc", now=_NOW)
        or ""
    )


def test_approval_lifetime_is_capped_and_bound_to_the_action() -> None:
    verifier = HmacApprovalVerifier(_SECRET)
    long_lived = _issue(verifier, expires_at="2999-01-01T00:00:00Z")
    assert "maximum" in (
        verifier.verify(long_lived, classification="write", action_digest="sha256:abc", now=_NOW)
        or ""
    )

    evidence = _issue(verifier)
    assert "exact requested action" in (
        verifier.verify(evidence, classification="write", action_digest="sha256:other", now=_NOW)
        or ""
    )
    assert "not currently valid" in (
        verifier.verify(
            evidence,
            classification="write",
            action_digest="sha256:abc",
            now="2026-10-04T19:00:00Z",
        )
        or ""
    )

    sensitive = _issue(
        verifier, classification="sensitive_write", expires_at="2026-10-04T18:10:00Z"
    )
    assert "maximum" in (
        verifier.verify(
            sensitive, classification="sensitive_write", action_digest="sha256:abc", now=_NOW
        )
        or ""
    )


def test_approval_secret_must_be_strong() -> None:
    with pytest.raises(ValueError, match="at least"):
        HmacApprovalVerifier(b"short")


# --- Finding 12: credentials, OAuth storage, audit redaction ------------------------------


def test_credential_references_cannot_be_shared_between_bindings() -> None:
    resolver = InMemoryCredentialResolver()
    resolver.register(
        local_identity_id="a",
        connection_id="c1",
        server_id="s",
        credential_ref="secret://shared",
        access_token="t",
        scopes=frozenset(),
    )
    with pytest.raises(ValueError, match="already bound"):
        resolver.register(
            local_identity_id="b",
            connection_id="c2",
            server_id="s",
            credential_ref="secret://shared",
            access_token="t",
            scopes=frozenset(),
        )


class _MemoryBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], object] = {}

    async def get(self, namespace: str, key: str) -> object | None:
        return self.values.get((namespace, key))

    async def set(self, namespace: str, key: str, value: object) -> None:
        self.values[(namespace, key)] = value


def test_oauth_tokens_that_miss_required_scopes_are_withheld() -> None:
    backend = _MemoryBackend()
    token = OAuthToken(access_token="abc", token_type="Bearer", scope="issues:read")
    backend.values[("ns", "tokens")] = token

    ok = NamespacedTokenStorage(backend, "ns", frozenset({"issues:read"}))
    assert asyncio.run(ok.get_tokens()) == token

    elevated = NamespacedTokenStorage(backend, "ns", frozenset({"issues:read", "issues:write"}))
    assert asyncio.run(elevated.get_tokens()) is None

    other = NamespacedTokenStorage(backend, "other-connection", frozenset())
    assert asyncio.run(other.get_tokens()) is None


def test_encrypted_oauth_storage_round_trips_and_hides_secrets(tmp_path: Path) -> None:
    key = EncryptedFileOAuthStorageBackend.generate_key()
    path = tmp_path / "oauth.bin"
    backend = EncryptedFileOAuthStorageBackend(path, key)
    token = OAuthToken(access_token="super-secret-access", token_type="Bearer", scope="a b")

    asyncio.run(backend.set("ns", "tokens", token))

    assert b"super-secret-access" not in path.read_bytes()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    restored = asyncio.run(EncryptedFileOAuthStorageBackend(path, key).get("ns", "tokens"))
    assert restored == token
    with pytest.raises(ValueError, match="decrypted"):
        asyncio.run(
            EncryptedFileOAuthStorageBackend(
                path, EncryptedFileOAuthStorageBackend.generate_key()
            ).get("ns", "tokens")
        )


def test_audit_redaction_covers_token_like_keys_and_values() -> None:
    redacted = _redact(
        {
            "token": "a",
            "api_key": "b",
            "idToken": "c",
            "nested": [{"Authorization": "Bearer abcdefghijkl"}],
            "message": "failed with Bearer abcdefghijklmnop and access_token=xyz123",
            "safe": "value",
        }
    )
    assert redacted == {
        "token": "[REDACTED]",
        "api_key": "[REDACTED]",
        "idToken": "[REDACTED]",
        "nested": [{"Authorization": "[REDACTED]"}],
        "message": "failed with Bearer [REDACTED] and access_token=[REDACTED]",
        "safe": "value",
    }


# --- Low-severity persistence hardening ---------------------------------------------------


def test_database_file_is_owner_only_and_operation_states_are_a_graph(tmp_path: Path) -> None:
    database = tmp_path / "adapter.db"
    with SqliteControlPlaneRepository(database) as repository:
        repository.initialize()
        assert stat.S_IMODE(os.stat(database).st_mode) == 0o600
        with pytest.raises(InvalidStateError, match="not allowed"):
            repository.transition_operation(
                "missing",
                expected_statuses=("COMPLETED",),
                status="RUNNING",
                updated_at=_NOW,
            )


def test_catalog_integrity_signature_detects_tampering(tmp_path: Path) -> None:
    database = tmp_path / "adapter.db"
    key = b"catalog-integrity-key-catalog-integrity"
    catalog = _compile(_tool(), _policy())
    with SqliteControlPlaneRepository(database, integrity_key=key) as repository:
        repository.initialize()
        from muse_mcp_adapter.application.control_plane_service import ControlPlaneService

        ControlPlaneService(repository, clock=lambda: _NOW).register_server(
            server_id="project-system",
            display_name="Project",
            endpoint="https://mcp.example.test/mcp",
            auth_profile="bearer_token",
        )
        repository.save_catalog(catalog)
        assert repository.get_catalog("project-system", "1.0.0").catalog_digest

        # An attacker with database access rewrites the catalog and recomputes its digest.
        repository._connection.execute(  # pyright: ignore[reportPrivateUsage]
            "UPDATE published_catalogs SET catalog_json = replace(catalog_json, 'write', 'read')"
        )
        with pytest.raises(InvalidStateError, match="integrity"):
            repository.get_catalog("project-system", "1.0.0")


def test_http_resources_must_declare_the_hosts_they_may_reach() -> None:
    primitive: dict[str, object] = {"type": "resource", "name": "site", "uri": "https://a.test/x"}
    base: dict[str, object] = {
        "primitiveKey": "resource:site",
        "allowed": True,
        "publicOperationId": "read_site",
        "classification": "read",
    }
    resource = {"allowedMimeTypes": ["text/plain"], "maxBytes": 100}
    with pytest.raises(CatalogValidationError):
        _compile(primitive, {**base, "resource": resource})
    with pytest.raises(CatalogValidationError):
        _compile(primitive, {**base, "resource": {**resource, "allowedUriHosts": ["b.test"]}})
    ok = _compile(primitive, {**base, "resource": {**resource, "allowedUriHosts": ["a.test"]}})
    assert ok.primitives[0].public_operation_id == "read_site"


def test_unparseable_numeric_hosts_are_rejected_without_a_dns_lookup() -> None:
    def forbidden(_host: str, _port: int) -> list[str]:
        raise AssertionError("numeric hosts must never be handed to the resolver")

    policy = EndpointSecurityPolicy(resolver=forbidden)
    for endpoint in ("https://1.2.3.4.5/mcp", "https://1.2.3.4.5.6/mcp"):
        with pytest.raises(UnsafeEndpointError):
            policy.validate(endpoint)
