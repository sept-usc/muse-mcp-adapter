"""Act like Muse against a running adapter, to check a connection end to end.

    python examples/simulate_muse.py link.json read '{"issue_id": "ISSUE-1"}' find_issue

``link.json`` is the output of ``muse-mcp-adapter link``. The script follows the same steps Muse
does when you ask it to create the connector: read the public spec, discover the sign-in
endpoints, register, sign in with the connection code, then call operations with the token.
"""

import base64
import hashlib
import json
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

REDIRECT = "https://muse.example.test/callback"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    link = json.loads(Path(argv[1]).read_text())
    calls = argv[2:]
    spec_url: str = link["connectorUrl"]
    base = spec_url.removesuffix("/openapi.json")

    with httpx.Client(timeout=30) as muse:
        spec = muse.get(spec_url).json()
        operations = sorted(
            path.rsplit("/", 1)[-1] for path in spec["paths"] if path.startswith("/operations/")
        )
        print("operations in the spec:", ", ".join(operations))

        origin = re.match(r"(https?://[^/]+)", base).group(1)  # type: ignore[union-attr]
        slug = base.rsplit("/c/", 1)[1]
        meta = muse.get(f"{origin}/.well-known/oauth-authorization-server/c/{slug}").json()
        client = muse.post(
            meta["registration_endpoint"],
            json={
                "client_name": "Muse (simulated)",
                "redirect_uris": [REDIRECT],
                "token_endpoint_auth_method": "none",
            },
        ).json()
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        page = muse.get(
            meta["authorization_endpoint"]
            + "?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": client["client_id"],
                    "redirect_uri": REDIRECT,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "state": "simulated",
                }
            )
        )
        fields = dict(re.findall(r"name=(\w+) value='([^']*)'", page.text))
        consent = muse.post(
            meta["issuer"] + "/oauth/consent",
            data={**fields, "code": link["connectionCode"]},
            follow_redirects=False,
        )
        location = consent.headers.get("location", "")
        if consent.status_code != 302:
            print("sign-in refused: the connection code was not accepted")
            return 1
        # An OAuth connection also needs the provider's own consent, in a real browser.
        if "/connect/" in location:
            print("Open this link in your browser to finish signing in to the provider:")
            print(" ", location)
            return 0
        code = parse_qs(urlsplit(location).query)["code"][0]
        tokens = muse.post(
            meta["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
                "code_verifier": verifier,
                "client_id": client["client_id"],
            },
        ).json()
        print("signed in; access token expires in", tokens["expires_in"], "seconds")

        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        # Calls come as: <label> <json arguments> <operation>, repeated.
        for index in range(0, len(calls) - 2, 3):
            label, arguments, operation = calls[index : index + 3]
            response = muse.post(
                f"{base}/operations/{operation}", json=json.loads(arguments), headers=headers
            )
            print(f"{label} {operation}: {response.status_code}")
            print(json.dumps(response.json(), indent=2)[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
