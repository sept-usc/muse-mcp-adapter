"""Muse signs in to the connector through the adapter's own OAuth server."""

import base64
from pathlib import Path

import pytest

from tests.fixtures.fake_muse import REDIRECT_URI, FakeMuse, pkce, query
from tests.fixtures.service_harness import PUBLIC_URL, Harness, harness


def _muse(h: Harness, slug: str) -> FakeMuse:
    return FakeMuse(h.http, slug)  # type: ignore[arg-type]


def test_discovery_documents_agree_with_each_other_and_with_the_spec(tmp_path: Path) -> None:
    with harness(tmp_path) as h:
        linked = h.link()
        slug = linked.record.slug
        issuer = f"{PUBLIC_URL}/c/{slug}"

        for path in (
            f"/.well-known/oauth-authorization-server/c/{slug}",
            f"/c/{slug}/.well-known/oauth-authorization-server",
            f"/.well-known/openid-configuration/c/{slug}",
            f"/c/{slug}/.well-known/openid-configuration",
        ):
            document = h.http.get(path).json()
            assert document["issuer"] == issuer
            assert document["authorization_endpoint"] == f"{issuer}/oauth/authorize"
            assert document["token_endpoint"] == f"{issuer}/oauth/token"
            assert document["registration_endpoint"] == f"{issuer}/oauth/register"
            assert document["code_challenge_methods_supported"] == ["S256"]
            assert document["response_types_supported"] == ["code"]

        for path in (
            f"/.well-known/oauth-protected-resource/c/{slug}",
            f"/c/{slug}/.well-known/oauth-protected-resource",
        ):
            resource = h.http.get(path).json()
            assert resource["resource"] == issuer
            assert resource["authorization_servers"] == [issuer]

        assert h.http.get("/.well-known/oauth-authorization-server/c/unknown").status_code == 404
        spec = h.http.get(f"/c/{slug}/openapi.json").json()
        flow = spec["components"]["securitySchemes"]["oauth2"]["flows"]["authorizationCode"]
        assert flow["authorizationUrl"] == f"{issuer}/oauth/authorize"
        assert flow["tokenUrl"] == f"{issuer}/oauth/token"
        assert spec["security"] == [{"oauth2": ["connector"]}, {"bearerAuth": []}]


def test_unauthenticated_calls_point_clients_at_the_discovery_document(tmp_path: Path) -> None:
    with harness(tmp_path) as h:
        slug = h.link().record.slug
        response = h.http.post(f"/c/{slug}/operations/find_issue", json={"id": "x"})
        assert response.status_code == 401
        challenge = response.headers["www-authenticate"]
        assert challenge.startswith("Bearer ")
        assert (
            f'resource_metadata="{PUBLIC_URL}/.well-known/oauth-protected-resource/c/{slug}"'
            in (challenge)
        )


def test_full_sign_in_then_calls_refresh_and_reuse_detection(tmp_path: Path) -> None:
    with harness(tmp_path) as h:
        linked = h.link()
        muse = _muse(h, linked.record.slug)
        assert muse.register().status_code == 201

        page = muse.authorize(pkce()[1])
        assert page.status_code == 200 and "connection code" in page.text.lower()
        assert page.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert page.headers["cache-control"] == "no-store"

        verifier, code, redirect = muse.sign_in(str(linked.link_code))
        assert redirect["state"] == "state-123"
        assert redirect["iss"] == f"{PUBLIC_URL}/c/{linked.record.slug}"

        tokens = muse.exchange(code, verifier).json()
        assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == 3600
        assert tokens["access_token"].startswith("mcpa_at_")
        assert h.call(tokens["access_token"], linked.record.slug) == 200

        refreshed = muse.refresh(tokens["refresh_token"]).json()
        assert refreshed["access_token"] != tokens["access_token"]
        assert h.call(refreshed["access_token"], linked.record.slug) == 200

        # presenting the already-used refresh token means it leaked: end the whole session
        reuse = muse.refresh(tokens["refresh_token"])
        assert reuse.status_code == 400 and reuse.json()["error"] == "invalid_grant"
        assert h.call(refreshed["access_token"], linked.record.slug) == 401
        assert muse.refresh(refreshed["refresh_token"]).status_code == 400


def test_the_static_token_keeps_working_alongside_oauth(tmp_path: Path) -> None:
    with harness(tmp_path) as h:
        linked = h.link()
        assert h.call(linked.token, linked.record.slug) == 200


def test_only_the_owner_with_the_connection_code_can_link(tmp_path: Path) -> None:
    with harness(tmp_path) as h:
        linked = h.link()
        muse = _muse(h, linked.record.slug)
        muse.register()
        _, challenge = pkce()
        page = muse.authorize(challenge)

        wrong = muse.consent(page, "AAAA-BBBB-CCCC")
        assert wrong.status_code == 401
        assert "location" not in wrong.headers  # no code is ever issued

        # the code is tolerant of case, spaces and missing hyphens
        sloppy = str(linked.link_code).lower().replace("-", " ")
        assert muse.consent(page, sloppy).status_code == 302


def test_repeated_wrong_codes_lock_the_connector_out(tmp_path: Path) -> None:
    with harness(tmp_path) as h:
        linked = h.link()
        muse = _muse(h, linked.record.slug)
        muse.register()
        page = muse.authorize(pkce()[1])

        assert [muse.consent(page, f"WRONG-{index}").status_code for index in range(5)] == [401] * 5
        locked = muse.consent(page, str(linked.link_code))  # even the right code is refused
        assert locked.status_code == 429

        h.clock.advance(901)  # the lockout window passes
        fresh = muse.authorize(pkce()[1])
        assert muse.consent(fresh, str(linked.link_code)).status_code == 302


def test_connection_codes_expire_and_can_be_reissued(tmp_path: Path) -> None:
    with harness(tmp_path) as h:
        linked = h.link()
        muse = _muse(h, linked.record.slug)
        muse.register()

        h.clock.advance(24 * 3600 + 1)
        page = muse.authorize(pkce()[1])
        assert muse.consent(page, str(linked.link_code)).status_code == 401

        fresh, _ = h.connections.issue_link_code(linked.record.connection_id)
        page = muse.authorize(pkce()[1])
        assert muse.consent(page, fresh).status_code == 302


def test_consent_needs_the_csrf_pair_and_a_live_request(tmp_path: Path) -> None:
    with harness(tmp_path) as h:
        linked = h.link()
        muse = _muse(h, linked.record.slug)
        muse.register()
        page = muse.authorize(pkce()[1])
        fields = {"request": "x", "csrf": "forged", "code": str(linked.link_code)}
        url = f"/c/{linked.record.slug}/oauth/consent"
        assert h.http.post(url, data=fields, follow_redirects=False).status_code == 400

        h.http.cookies.clear()
        missing = h.http.post(url, data={"code": str(linked.link_code)}, follow_redirects=False)
        assert missing.status_code == 400

        page = muse.authorize(pkce()[1])
        h.clock.advance(601)  # the authorize request itself expires
        assert muse.consent(page, str(linked.link_code)).status_code == 400


def test_untrusted_clients_and_redirects_are_never_redirected_to(tmp_path: Path) -> None:
    with harness(tmp_path) as h:
        linked = h.link()
        muse = _muse(h, linked.record.slug)
        muse.register()
        challenge = pkce()[1]

        bad_redirect = muse.authorize(challenge, redirect_uri="https://evil.example/cb")
        assert bad_redirect.status_code == 400 and "location" not in bad_redirect.headers
        near_miss = muse.authorize(challenge, redirect_uri=REDIRECT_URI + "/")
        assert near_miss.status_code == 400
        unknown = muse.authorize(challenge, client_id="mcpc_nope")
        assert unknown.status_code == 400 and "location" not in unknown.headers


@pytest.mark.parametrize(
    ("override", "error"),
    [
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge": "short"}, "invalid_request"),
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"scope": "connector admin"}, "invalid_scope"),
        ({"resource": "https://other.example/c/x"}, "invalid_target"),
        ({"state": "s" * 600}, "invalid_request"),
    ],
)
def test_protocol_errors_go_back_to_the_registered_client(
    tmp_path: Path, override: dict[str, str], error: str
) -> None:
    with harness(tmp_path) as h:
        muse = _muse(h, h.link().record.slug)
        muse.register()
        response = muse.authorize(pkce()[1], **override)
        assert response.status_code == 302
        sent = query(response.headers["location"])
        assert response.headers["location"].startswith(REDIRECT_URI)
        assert sent["error"] == error


def test_pkce_binds_the_code_to_the_party_that_started_the_flow(tmp_path: Path) -> None:
    with harness(tmp_path) as h:
        linked = h.link()
        muse = _muse(h, linked.record.slug)
        muse.register()
        verifier, code, _ = muse.sign_in(str(linked.link_code))

        wrong = muse.exchange(code, pkce()[0])  # an interceptor does not know the verifier
        assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_grant"
        # the code is spent by the failed attempt, so even the right verifier is too late
        assert muse.exchange(code, verifier).status_code == 400


def test_a_replayed_code_revokes_what_it_produced(tmp_path: Path) -> None:
    with harness(tmp_path) as h:
        linked = h.link()
        muse = _muse(h, linked.record.slug)
        muse.register()
        verifier, code, _ = muse.sign_in(str(linked.link_code))
        tokens = muse.exchange(code, verifier).json()
        assert h.call(tokens["access_token"], linked.record.slug) == 200

        assert muse.exchange(code, verifier).status_code == 400
        assert h.call(tokens["access_token"], linked.record.slug) == 401


def test_codes_and_tokens_expire(tmp_path: Path) -> None:
    with harness(tmp_path) as h:
        linked = h.link()
        muse = _muse(h, linked.record.slug)
        muse.register()

        verifier, code, _ = muse.sign_in(str(linked.link_code))
        h.clock.advance(61)
        assert muse.exchange(code, verifier).status_code == 400  # codes live one minute

        verifier, code, _ = muse.sign_in(str(linked.link_code))
        tokens = muse.exchange(code, verifier).json()
        h.clock.advance(3601)
        assert h.call(tokens["access_token"], linked.record.slug) == 401
        renewed = muse.refresh(tokens["refresh_token"]).json()
        assert h.call(renewed["access_token"], linked.record.slug) == 200

        h.clock.advance(31 * 24 * 3600)
        assert muse.refresh(renewed["refresh_token"]).status_code == 400


def test_redirect_and_client_must_match_at_the_token_endpoint(tmp_path: Path) -> None:
    with harness(tmp_path) as h:
        linked = h.link()
        muse = _muse(h, linked.record.slug)
        muse.register(redirect_uris=[REDIRECT_URI, "https://muse.example.test/other"])
        verifier, code, _ = muse.sign_in(str(linked.link_code))
        mismatch = muse.exchange(code, verifier, redirect_uri="https://muse.example.test/other")
        assert mismatch.status_code == 400

        other = FakeMuse(h.http, linked.record.slug)  # type: ignore[arg-type]
        other.register()
        verifier, code, _ = muse.sign_in(str(linked.link_code))
        stolen = other.exchange(code, verifier)  # another client presents the code
        assert stolen.status_code == 400


@pytest.mark.parametrize(
    "override",
    [
        {"redirect_uris": ["http://evil.example/cb"]},
        {"redirect_uris": ["https://muse.example.test/cb#frag"]},
        {"redirect_uris": ["https://user:pw@muse.example.test/cb"]},
        {"redirect_uris": ["javascript:alert(1)"]},
        {"redirect_uris": []},
        {"redirect_uris": [f"https://muse.example.test/{i}" for i in range(6)]},
        {"token_endpoint_auth_method": "private_key_jwt"},
        {"grant_types": ["password"]},
        {"response_types": ["token"]},
    ],
)
def test_client_registration_rejects_unsafe_metadata(
    tmp_path: Path, override: dict[str, object]
) -> None:
    with harness(tmp_path) as h:
        muse = _muse(h, h.link().record.slug)
        response = muse.register(**override)
        assert response.status_code == 400, response.text
        assert response.json()["error"] in {"invalid_redirect_uri", "invalid_client_metadata"}


def test_loopback_http_redirects_are_allowed_and_registrations_are_capped(
    tmp_path: Path,
) -> None:
    with harness(tmp_path) as h:
        muse = _muse(h, h.link().record.slug)
        assert muse.register(redirect_uris=["http://127.0.0.1:8123/cb"]).status_code == 201
        for _ in range(19):
            assert muse.register().status_code == 201
        assert muse.register().status_code == 429


def test_confidential_clients_must_authenticate(tmp_path: Path) -> None:
    with harness(tmp_path) as h:
        linked = h.link()
        muse = _muse(h, linked.record.slug)
        assert muse.register(token_endpoint_auth_method="client_secret_post").status_code == 201
        secret = muse.client["client_secret"]

        verifier, code, _ = muse.sign_in(str(linked.link_code))
        missing = muse.exchange(code, verifier)
        assert missing.status_code == 401 and missing.json()["error"] == "invalid_client"

        verifier, code, _ = muse.sign_in(str(linked.link_code))
        assert muse.exchange(code, verifier, client_secret="wrong").status_code == 401
        verifier, code, _ = muse.sign_in(str(linked.link_code))
        assert muse.exchange(code, verifier, client_secret=secret).status_code == 200

        basic = base64.b64encode(f"{muse.client['client_id']}:{secret}".encode()).decode()
        verifier, code, _ = muse.sign_in(str(linked.link_code))
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        }
        via_basic = h.http.post(
            f"/c/{linked.record.slug}/oauth/token",
            data=data,
            headers={"Authorization": f"Basic {basic}"},
        )
        assert via_basic.status_code == 200


def test_connectors_are_isolated_from_each_other(tmp_path: Path) -> None:
    with harness(tmp_path) as h:
        first, second = h.link("first"), h.link("second")
        muse = _muse(h, first.record.slug)
        muse.register()
        verifier, code, _ = muse.sign_in(str(first.link_code))
        tokens = muse.exchange(code, verifier).json()

        # the first connector's token does not open the second connector
        assert h.call(tokens["access_token"], first.record.slug) == 200
        assert h.call(tokens["access_token"], second.record.slug) == 401
        # nor can its client or code be used there
        elsewhere = FakeMuse(h.http, second.record.slug)  # type: ignore[arg-type]
        elsewhere.client = muse.client
        verifier, code, _ = muse.sign_in(str(first.link_code))
        assert elsewhere.exchange(code, verifier).status_code == 401
        page = elsewhere.authorize(pkce()[1])
        assert page.status_code == 400  # the second connector has never heard of this client


def test_unlinking_or_resetting_a_connection_ends_every_oauth_session(tmp_path: Path) -> None:
    with harness(tmp_path) as h:
        linked = h.link()
        muse = _muse(h, linked.record.slug)
        muse.register()
        verifier, code, _ = muse.sign_in(str(linked.link_code))
        tokens = muse.exchange(code, verifier).json()
        assert h.call(tokens["access_token"], linked.record.slug) == 200

        rotated = h.connections.rotate_token(linked.record.connection_id)
        assert h.call(tokens["access_token"], linked.record.slug) == 401
        assert muse.refresh(tokens["refresh_token"]).status_code == 400

        # the old connection code died with the reset; the new one signs in again
        stale = muse.consent(muse.authorize(pkce()[1]), str(linked.link_code))
        assert stale.status_code == 401
        verifier, code, _ = muse.sign_in(str(rotated.link_code))
        again = muse.exchange(code, verifier).json()
        assert h.call(again["access_token"], linked.record.slug) == 200
        h.connections.unlink(linked.record.connection_id)
        assert h.call(again["access_token"], linked.record.slug) == 401


def test_revocation_endpoint_ends_the_session(tmp_path: Path) -> None:
    with harness(tmp_path) as h:
        linked = h.link()
        muse = _muse(h, linked.record.slug)
        muse.register()
        verifier, code, _ = muse.sign_in(str(linked.link_code))
        tokens = muse.exchange(code, verifier).json()

        response = h.http.post(
            f"/c/{linked.record.slug}/oauth/revoke",
            data={"token": tokens["refresh_token"], "client_id": muse.client["client_id"]},
        )
        assert response.status_code == 200
        assert h.call(tokens["access_token"], linked.record.slug) == 401


def test_secrets_are_not_stored_in_clear(tmp_path: Path) -> None:
    with harness(tmp_path) as h:
        linked = h.link()
        muse = _muse(h, linked.record.slug)
        muse.register(token_endpoint_auth_method="client_secret_post")
        verifier, code, _ = muse.sign_in(str(linked.link_code))
        secret = muse.client["client_secret"]
        tokens = muse.exchange(code, verifier, client_secret=secret).json()

        dump = h.repository.run(
            lambda db: "\n".join(
                str(tuple(row))
                for table in (
                    "oauth_clients",
                    "oauth_codes",
                    "oauth_tokens",
                    "oauth_auth_requests",
                    "connections",
                )
                for row in db.execute(f"SELECT * FROM {table}")  # fixed table names
            )
        )
        for secret_value in (
            code,
            secret,
            tokens["access_token"],
            tokens["refresh_token"],
            linked.token,
            str(linked.link_code),
        ):
            assert secret_value not in dump
