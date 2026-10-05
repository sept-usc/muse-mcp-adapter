import pytest

from muse_mcp_adapter.discovery.client import ManifestDiscoveryClient
from muse_mcp_adapter.domain.errors import RequestValidationError
from muse_mcp_adapter.domain.models import ServerRegistration
from muse_mcp_adapter.providers import (
    ProviderConformanceRunner,
    build_endpoint,
    load_provider_pack,
    provider_pack_ids,
)
from muse_mcp_adapter.transport.sdk_client import McpSdkClient
from tests.fixtures.phase_one import discovery_manifest


def test_all_reference_provider_packs_use_the_generic_runtime() -> None:
    runtime_type = type(McpSdkClient())
    packs = [load_provider_pack(pack_id) for pack_id in provider_pack_ids()]

    assert [pack.pack_id for pack in packs] == ["atlassian", "linear", "supabase"]
    assert all(type(McpSdkClient()) is runtime_type for _ in packs)
    assert all(pack.safety["classificationMode"] == "explicit_customer_policy" for pack in packs)


def test_all_reference_packs_pass_the_same_discovery_conformance_runner() -> None:
    server = ServerRegistration(
        server_id="provider-conformance",
        display_name="Provider Conformance",
        endpoint="https://mcp.example.test/mcp",
        auth_profile="oauth_pkce",
        provider_pack=None,
        enabled=True,
        created_at="2026-10-05T00:00:00Z",
        updated_at="2026-10-05T00:00:00Z",
    )
    snapshot = ManifestDiscoveryClient(
        discovery_manifest(), discovered_at="2026-10-05T00:00:00Z"
    ).discover(server)
    runner = ProviderConformanceRunner()

    results = [
        runner.evaluate(load_provider_pack(pack_id), snapshot, auth_mode="oauth_pkce")
        for pack_id in provider_pack_ids()
    ]
    assert all(result.passed for result in results)


def test_linear_read_only_profile_and_supabase_safe_defaults() -> None:
    linear = load_provider_pack("linear")
    assert build_endpoint(linear, profile="read_only") == ("https://mcp.linear.app/mcp/readonly")

    supabase = load_provider_pack("supabase")
    endpoint = build_endpoint(
        supabase,
        options={"project_ref": "project-123", "features": "database,docs"},
    )
    assert endpoint == (
        "https://mcp.supabase.com/mcp?read_only=true&project_ref=project-123"
        "&features=database%2Cdocs"
    )


def test_provider_pack_rejects_missing_or_unknown_options() -> None:
    supabase = load_provider_pack("supabase")
    with pytest.raises(RequestValidationError, match="Missing provider options"):
        build_endpoint(supabase)
    with pytest.raises(RequestValidationError, match="Unknown provider options"):
        build_endpoint(supabase, options={"project_ref": "p", "unsafe": "true"})
