"""A stand-in for Muse's OAuth client: discovery, registration, PKCE and token exchange."""

import base64
import hashlib
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

REDIRECT_URI = "https://muse.example.test/oauth/callback"


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.rstrip(b"=").decode()


def hidden_fields(html: str) -> dict[str, str]:
    return dict(re.findall(r"name=(\w+) value='([^']*)'", html))


def query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


class FakeMuse:
    def __init__(self, http: httpx.Client, slug: str) -> None:
        self.http = http
        self.slug = slug
        self.base = f"/c/{slug}"
        self.client: dict[str, Any] = {}

    def register(self, **overrides: Any) -> httpx.Response:
        body = {
            "client_name": "Muse",
            "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        } | overrides
        response = self.http.post(f"{self.base}/oauth/register", json=body)
        if response.status_code == 201:
            self.client = response.json()
        return response

    def authorize(self, challenge: str, **overrides: str) -> httpx.Response:
        params = {
            "response_type": "code",
            "client_id": self.client["client_id"],
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "state-123",
            "scope": "connector",
        } | overrides
        return self.http.get(
            f"{self.base}/oauth/authorize?{urlencode(params)}", follow_redirects=False
        )

    def consent(self, page: httpx.Response, code: str) -> httpx.Response:
        fields = hidden_fields(page.text)
        return self.http.post(
            f"{self.base}/oauth/consent", data={**fields, "code": code}, follow_redirects=False
        )

    def exchange(self, code: str, verifier: str, **overrides: str) -> httpx.Response:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
            "client_id": self.client["client_id"],
        } | overrides
        return self.http.post(f"{self.base}/oauth/token", data=data)

    def refresh(self, refresh_token: str, **overrides: str) -> httpx.Response:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client["client_id"],
        } | overrides
        return self.http.post(f"{self.base}/oauth/token", data=data)

    def sign_in(self, link_code: str, **overrides: str) -> tuple[str, str, dict[str, str]]:
        """Run authorize and consent; returns (verifier, authorization code, redirect query)."""
        verifier, challenge = pkce()
        page = self.authorize(challenge, **overrides)
        assert page.status_code == 200, page.text
        redirected = self.consent(page, link_code)
        assert redirected.status_code == 302, redirected.text
        parameters = query(redirected.headers["location"])
        return verifier, parameters["code"], parameters
