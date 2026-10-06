"""Delegated OAuth to an MCP provider, end to end over real sockets.

A fake authorization server and protected MCP server run locally. The adapter runs as the
real HTTP service. An httpx client with a cookie jar plays the owner's browser.
"""

import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from muse_mcp_adapter.application.control_plane_service import ControlPlaneService
from muse_mcp_adapter.application.serve import ServeConfig, build_application
from muse_mcp_adapter.connections import ConnectionService
from muse_mcp_adapter.discovery.client import ManifestDiscoveryClient
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from muse_mcp_adapter.transport.security import EndpointSecurityPolicy
from tests.fixtures.fake_muse import REDIRECT_URI, FakeMuse, pkce, query
from tests.fixtures.fake_oauth import FakeAuthorizationServer, protected_server
from tests.fixtures.live_server import free_port, run_asgi

_LOCAL = EndpointSecurityPolicy.local_development()


@dataclass
class World:
    adapter: str
    provider: FakeAuthorizationServer
    connections: ConnectionService
    repository: SqliteControlPlaneRepository

    def call(self, token: str, slug: str, operation: str = "find_issue") -> httpx.Response:
        return httpx.post(
            f"{self.adapter}/c/{slug}/operations/{operation}",
            json={"issue_id": "ISSUE-1"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )

    def link(self, label: str = "owner phone", scopes: list[str] | None = None):  # type: ignore[no-untyped-def]
        return self.connections.link(
            server_id="protected",
            label=label,
            credential_kind="oauth",
            scopes=scopes or ["issues:read"],
        )


@contextmanager
def _world(tmp_path: Path, *, access_ttl: int = 3600) -> Generator[World]:
    provider = FakeAuthorizationServer(access_ttl_seconds=access_ttl)
    provider_port, adapter_port = free_port(), free_port()
    manifest = {
        "protocolVersion": "2025-11-25",
        "serverInfo": {"name": "Protected"},
        "capabilities": {},
        "primitives": [
            {
                "type": "tool",
                "name": "find_issue",
                "inputSchema": {
                    "type": "object",
                    "properties": {"issue_id": {"type": "string"}},
                    "required": ["issue_id"],
                },
                "annotations": {"readOnlyHint": True},
                "requiredScopes": ["issues:read"],
            }
        ],
    }
    database = tmp_path / "adapter.db"
    with SqliteControlPlaneRepository(database) as repository:
        repository.initialize()
        control = ControlPlaneService(repository, endpoint_policy=_LOCAL)
        control.register_server(
            server_id="protected",
            display_name="Protected",
            endpoint=f"http://127.0.0.1:{provider_port}/mcp",
            auth_profile="oauth",
        )
        control.discover("protected", ManifestDiscoveryClient(manifest))
        control.publish_catalog(
            server_id="protected",
            catalog_version="1.0.0",
            policies=[
                {
                    "primitiveKey": "tool:find_issue",
                    "allowed": True,
                    "publicOperationId": "find_issue",
                    "classification": "read",
                    "requiredScopes": ["issues:read"],
                    "maxResultBytes": 10_000,
                }
            ],
        )
        control.activate_catalog("protected", "1.0.0")
        config = ServeConfig(
            public_url=f"http://127.0.0.1:{adapter_port}",
            allow_insecure_http=True,
            allow_private_networks=True,
        )
        app = build_application(repository, config, {}, database=database)
        server = protected_server(provider, provider_port)
        with (
            run_asgi(server.streamable_http_app(), provider_port),
            run_asgi(app, adapter_port),
        ):
            yield World(
                adapter=config.public_url,
                provider=provider,
                connections=ConnectionService(repository),
                repository=repository,
            )


def _browser_login(url: str) -> httpx.Response:
    """Open a sign-in link as the owner's browser would, following every redirect."""
    with httpx.Client(follow_redirects=True, timeout=30) as browser:
        return browser.get(url)


def test_owner_signs_in_once_and_calls_work_afterwards(tmp_path: Path) -> None:
    with _world(tmp_path) as world:
        linked = world.link()
        slug, token = linked.record.slug, linked.token

        before = world.call(token, slug)
        assert before.status_code == 401
        assert before.json()["error"] == "REAUTHORIZATION_REQUIRED"
        link = before.json()["authorizationUrl"]
        assert "/connect/" in link and before.json()["message"].endswith(link)

        page = _browser_login(link)
        assert page.status_code == 200 and "connected" in page.text.lower()

        after = world.call(token, slug)
        assert after.status_code == 200, after.text
        assert after.json()["structuredContent"] == {"id": "ISSUE-1", "title": "Example"}
        # the granted scopes are recorded for later checks
        assert world.repository.get_connection(linked.record.connection_id).scopes == (
            "issues:read",
        )
        # PKCE and the registered callback were used
        authorization = world.provider.authorizations[0]
        assert authorization.code_challenge
        assert str(authorization.redirect_uri) == f"{world.adapter}/oauth/callback"


def test_expired_access_tokens_refresh_without_the_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("muse_mcp_adapter.transport.oauth._EXPIRY_SKEW_SECONDS", 0)
    with _world(tmp_path, access_ttl=2) as world:
        linked = world.link()
        _browser_login(world.call(linked.token, linked.record.slug).json()["authorizationUrl"])
        assert world.call(linked.token, linked.record.slug).status_code == 200

        time.sleep(2.3)  # the access token really expires at the provider
        before = world.provider.token_requests
        assert world.call(linked.token, linked.record.slug).status_code == 200
        assert world.provider.token_requests == before + 1  # refreshed silently, no sign-in


def test_lost_refresh_token_asks_the_owner_to_reconnect(tmp_path: Path) -> None:
    with _world(tmp_path) as world:
        linked = world.link()
        _browser_login(world.call(linked.token, linked.record.slug).json()["authorizationUrl"])
        world.provider.revoke_everything()
        world.provider.expire_access_tokens()

        again = world.call(linked.token, linked.record.slug)
        assert again.status_code == 401
        assert again.json()["error"] == "REAUTHORIZATION_REQUIRED"
        assert _browser_login(again.json()["authorizationUrl"]).status_code == 200
        assert world.call(linked.token, linked.record.slug).status_code == 200


def test_each_connection_keeps_its_own_tokens(tmp_path: Path) -> None:
    with _world(tmp_path) as world:
        first, second = world.link("first"), world.link("second")
        _browser_login(world.call(first.token, first.record.slug).json()["authorizationUrl"])

        assert world.call(first.token, first.record.slug).status_code == 200
        other = world.call(second.token, second.record.slug)
        assert other.status_code == 401  # the first sign-in is not shared
        assert other.json()["error"] == "REAUTHORIZATION_REQUIRED"


def test_a_token_only_works_for_its_own_connector_url(tmp_path: Path) -> None:
    with _world(tmp_path) as world:
        first, second = world.link("first"), world.link("second")
        crossed = world.call(first.token, second.record.slug)
        assert crossed.status_code == 401
        assert crossed.json()["error"] == "AUTHENTICATION_FAILED"


def test_sign_in_links_are_single_use_and_callbacks_need_the_same_browser(
    tmp_path: Path,
) -> None:
    with _world(tmp_path) as world:
        linked = world.link()
        link = world.call(linked.token, linked.record.slug).json()["authorizationUrl"]

        # Start the flow in one browser but deliver the provider's answer in another.
        with httpx.Client(timeout=30) as owner, httpx.Client(timeout=30) as attacker:
            start = owner.get(link)
            assert start.status_code == 302
            provider_page = owner.get(start.headers["location"])  # provider redirects back
            callback = provider_page.headers["location"]
            assert callback.startswith(f"{world.adapter}/oauth/callback")

            stolen = attacker.get(callback)  # no cookie from the owner's browser
            assert stolen.status_code == 400 and "not started in this browser" in stolen.text

            replay = owner.get(link)  # the link was already spent
            assert replay.status_code == 404

            genuine = owner.get(callback)
            assert genuine.status_code == 200 and "connected" in genuine.text.lower()
        assert world.call(linked.token, linked.record.slug).status_code == 200


def test_forged_state_and_error_callbacks_are_rejected(tmp_path: Path) -> None:
    with _world(tmp_path) as world:
        forged = httpx.get(f"{world.adapter}/oauth/callback?code=x&state=nope", timeout=30)
        assert forged.status_code == 400

        linked = world.link()
        link = world.call(linked.token, linked.record.slug).json()["authorizationUrl"]
        with httpx.Client(timeout=30, follow_redirects=False) as browser:
            start = browser.get(link)
            state = httpx.URL(start.headers["location"]).params["state"]
            denied = browser.get(
                f"{world.adapter}/oauth/callback?error=access_denied&state={state}"
            )
            assert denied.status_code == 400 and "not connected" in denied.text.lower()
        assert world.call(linked.token, linked.record.slug).status_code == 401


def test_unauthenticated_responses_never_leak_tokens_or_codes(tmp_path: Path) -> None:
    with _world(tmp_path) as world:
        linked = world.link()
        body = world.call(linked.token, linked.record.slug).text
        assert linked.token not in body
        assert "code=" not in body


def _follow_to(browser: httpx.Client, url: str, stop: str) -> str:
    """Follow redirects by hand until the next location starts with ``stop``."""
    for _ in range(12):
        response = browser.get(url, follow_redirects=False)
        assert response.status_code == 302, response.text
        url = response.headers["location"]
        if url.startswith(stop) or stop in url:
            return url
    raise AssertionError("too many redirects")


def test_muse_sign_in_chains_through_the_providers_consent(tmp_path: Path) -> None:
    with _world(tmp_path) as world:
        linked = world.link()
        slug = linked.record.slug
        with httpx.Client(base_url=world.adapter, timeout=30) as browser:
            muse = FakeMuse(browser, slug)
            assert muse.register().status_code == 201
            verifier, challenge = pkce()
            consent = muse.consent(muse.authorize(challenge), str(linked.link_code))
            assert consent.status_code == 302
            assert "/connect/" in consent.headers["location"]  # the provider comes first

            back = _follow_to(browser, consent.headers["location"], "/oauth/continue")
            assert f"/c/{slug}/oauth/continue?req=" in back

            # the provider's answer only counts in the browser that started the sign-in
            with httpx.Client(timeout=30) as attacker:
                assert attacker.get(back, follow_redirects=False).status_code == 400
            delivered = browser.get(back, follow_redirects=False)
            assert delivered.status_code == 302
            assert delivered.headers["location"].startswith(REDIRECT_URI)
            params = query(delivered.headers["location"])
            assert params["state"] == "state-123"

            tokens = muse.exchange(params["code"], verifier).json()
            # Muse's token works, and the adapter uses the provider tokens behind it
            ok = world.call(tokens["access_token"], slug)
            assert ok.status_code == 200, ok.text
            assert ok.json()["structuredContent"]["id"] == "ISSUE-1"
            # the continuation was single-use
            assert browser.get(back, follow_redirects=False).status_code == 400


def test_second_muse_sign_in_skips_the_provider_when_already_connected(tmp_path: Path) -> None:
    with _world(tmp_path) as world:
        linked = world.link()
        _browser_login(world.call(linked.token, linked.record.slug).json()["authorizationUrl"])
        with httpx.Client(base_url=world.adapter, timeout=30) as browser:
            muse = FakeMuse(browser, linked.record.slug)
            muse.register()
            verifier, challenge = pkce()
            consent = muse.consent(muse.authorize(challenge), str(linked.link_code))
            assert consent.headers["location"].startswith(REDIRECT_URI)
            code = query(consent.headers["location"])["code"]
            tokens = muse.exchange(code, verifier).json()
            assert world.call(tokens["access_token"], linked.record.slug).status_code == 200


@pytest.mark.parametrize("wait", [0])
def test_world_starts_quickly(tmp_path: Path, wait: int) -> None:
    started = time.monotonic()
    with _world(tmp_path):
        pass
    assert time.monotonic() - started < 10 + wait
