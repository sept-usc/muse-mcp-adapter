"""Running the adapter's own administration commands on the VM.

Everything here is `docker compose exec adapter muse-mcp-adapter ...` on the target. The adapter
keeps doing what it already does (register, discover, review, publish, link); this layer only
carries the commands there and the answers back.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from typing import Any, cast

from .runner import CommandError, Result
from .targets import Target

SERVER_ID = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
BASE = ["muse-mcp-adapter", "--database", "/data/adapter.db", "--config", "/config/serve.json"]


def check_server_id(server_id: str) -> str:
    if not SERVER_ID.match(server_id):
        raise SystemExit(
            "A server id is 3 to 64 characters: lowercase letters, digits and '-', "
            "starting with a letter."
        )
    return server_id


@dataclass
class Linked:
    """What `link` prints once: the connector URL, the token, the code, and any sign-in link."""

    connector_url: str
    token: str
    connection_code: str
    connection_id: str
    login_url: str | None
    muse_instructions: str


class Admin:
    def __init__(self, target: Target, actor: str = "muse-vm", dev_network: bool = False) -> None:
        self.target, self.actor, self.dev_network = target, actor, dev_network

    # ---- plumbing ----------------------------------------------------------------------------
    def run(self, *args: str, check: bool = True, timeout: float = 180) -> Result:
        flags = ["--actor", self.actor]
        if self.dev_network:  # only for trying the adapter against servers on a private network
            flags += ["--allow-http", "--allow-private-network"]
        words = [*BASE, *flags, *args]
        return self.target.compose(
            "exec -T adapter " + " ".join(shlex.quote(word) for word in words),
            check=check,
            timeout=timeout,
        )

    def json(self, *args: str, timeout: float = 180) -> Any:
        result = self.run(*args, timeout=timeout)
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise CommandError(["muse-mcp-adapter", *args], result) from None

    # ---- the steps ---------------------------------------------------------------------------
    def register(self, server_id: str, name: str, endpoint: str, auth_profile: str) -> None:
        check_server_id(server_id)
        self.run(
            "server-register",
            "--server-id",
            server_id,
            "--display-name",
            name,
            "--endpoint",
            endpoint,
            "--auth-profile",
            auth_profile,
        )

    def discover(
        self, server_id: str, *, token_env: str | None = None, connection_id: str | None = None
    ) -> int:
        args = ["discover-remote", "--server-id", server_id]
        if token_env:
            args += ["--token-env", token_env]
        if connection_id:
            args += ["--connection-id", connection_id]
        return int(self.json(*args, timeout=300).get("primitiveCount", 0))

    def draft_policy(self, server_id: str, *, expose_reads: bool = True) -> list[dict[str, Any]]:
        args = ["policy-template", "--server-id", server_id]
        if expose_reads:
            args.append("--expose-reads")
        return cast(list[dict[str, Any]], self.json(*args))

    def publish(self, server_id: str, version: str, policies: list[dict[str, Any]]) -> None:
        path = f"data/policies/{server_id}-{version}.json"
        self.target.put(path, json.dumps(policies, indent=2), 0o600, owner="10001:10001")
        try:
            self.run(
                "publish",
                "--server-id",
                server_id,
                "--catalog-version",
                version,
                "--policies",
                "/" + path,
            )
            self.run("activate", "--server-id", server_id, "--catalog-version", version)
        finally:
            self.target.sh(f"rm -f {shlex.quote(path)}", check=False)

    def published(self, server_id: str) -> tuple[str, dict[str, dict[str, Any]]]:
        """The live catalog's version and the policy of each primitive in it."""
        data = self.status(server_id)
        active = cast(dict[str, Any], data.get("activeCatalog") or {})
        policies: dict[str, dict[str, Any]] = {}
        for item in cast(list[dict[str, Any]], active.get("primitives", [])):
            policy = item.get("policy")
            if isinstance(policy, dict):
                typed = cast(dict[str, Any], policy)
                policies[str(typed["primitiveKey"])] = typed
        return str(active.get("catalogVersion", "")), policies

    def link(self, server_id: str, label: str, credential: str, token_env: str | None) -> Linked:
        args = ["link", "--server-id", server_id, "--label", label, "--credential", credential]
        if token_env:
            args += ["--token-env", token_env]
        data = cast(dict[str, Any], self.json(*args))
        return Linked(
            connector_url=str(data["connectorUrl"]),
            token=str(data["token"]),
            connection_code=str(data["connectionCode"]),
            connection_id=str(data["connectionId"]),
            login_url=str(data["loginUrl"]) if data.get("loginUrl") else None,
            muse_instructions=str(data.get("museInstructions", "")),
        )

    # ---- looking after it --------------------------------------------------------------------
    def links(self) -> list[dict[str, Any]]:
        data = self.json("links")
        return cast(
            list[dict[str, Any]], data if isinstance(data, list) else data.get("connections", [])
        )

    def status(self, server_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.json("status", "--server-id", server_id))

    def servers(self) -> list[dict[str, Any]]:
        data = self.json("server-list")
        return cast(
            list[dict[str, Any]], data if isinstance(data, list) else data.get("servers", [])
        )

    def drift(self, server_id: str) -> Any:
        return self.json("drift", "--server-id", server_id)

    def rotate_token(self, connection_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.json("rotate-token", "--connection-id", connection_id))

    def unlink(self, connection_id: str) -> None:
        self.run("unlink", "--connection-id", connection_id)

    def login_link(self, connection_id: str) -> str:
        data = cast(dict[str, Any], self.json("login", "--connection-id", connection_id))
        return str(data.get("loginUrl") or data.get("url") or "")


def pick_exposure(policies: list[dict[str, Any]], *, allow: list[str] | None = None) -> list[str]:
    """Turn on the named tools in a drafted policy. Writes stay classified as writes."""
    wanted = set(allow or [])
    known = {str(item["primitiveKey"]).split(":", 1)[1] for item in policies}
    missing = wanted - known
    if missing:
        raise SystemExit(
            f"The server offers no such tool: {', '.join(sorted(missing))}. "
            f"It offers: {', '.join(sorted(known))}."
        )
    exposed: list[str] = []
    for item in policies:
        name = str(item["primitiveKey"]).split(":", 1)[1]
        if name in wanted:
            item["allowed"] = True
        if item.get("allowed") is True:
            exposed.append(f"{item['primitiveKey']} [{item.get('classification', '?')}]")
    return exposed
