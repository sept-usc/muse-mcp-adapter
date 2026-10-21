"""muse-vm.toml: everything `muse-vm setup` asks, written down once.

Every key is optional. A person at a terminal is asked for what is missing; a script gets an error
that names it. Unknown keys are rejected, so a typo never silently changes nothing.

    name = "prod"
    cloud = "gcp"              # or: ssh = "deploy@203.0.113.7", or: local = true
    project = "my-project"
    yes = true                 # accept that this creates billable resources
    [[servers]]
    preset = "github"
    allow = ["issue_write"]    # tools beyond the read-only ones
    token_file = "~/github-token"
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

FILENAME = "muse-vm.toml"

_TOP = {
    "name": str,
    "cloud": str,
    "ssh": str,
    "local": bool,
    "ssh_opts": list,
    "project": str,
    "zone": str,
    "machine_type": str,
    "budget_usd": (int, float),
    "idle_stop_hours": int,
    "egress_allow": list,
    "tz": str,
    "tailscale_authkey_file": str,
    "yes": bool,
    "servers": list,
}
_SERVER = {
    "preset": str,
    "id": str,
    "endpoint": str,
    "credential": str,
    "secret_name": str,
    "token_file": str,
    "allow": list,
    "no_reads": bool,
}


class ConfigError(ValueError):
    pass


@dataclass
class ServerPlan:
    server: str  # a well-known name, or an id of your own (then `endpoint` is needed)
    endpoint: str | None = None
    credential: str | None = None
    secret_name: str | None = None
    token_file: str | None = None
    allow: list[str] = field(default_factory=list[str])
    no_reads: bool = False


@dataclass
class Plan:
    name: str | None = None
    cloud: str | None = None
    ssh: str | None = None
    local: bool = False
    ssh_opts: list[str] = field(default_factory=list[str])
    project: str | None = None
    zone: str | None = None
    machine_type: str | None = None
    budget_usd: float | None = None
    idle_stop_hours: int | None = None
    egress_allow: list[str] = field(default_factory=list[str])
    tz: str | None = None
    tailscale_authkey_file: str | None = None
    yes: bool = False
    # not a file setting: removing what an earlier run left is chosen at the time, never stored
    replace_leftovers: bool = False
    servers: list[ServerPlan] = field(default_factory=list[ServerPlan])


def _check(table: dict[str, Any], allowed: dict[str, Any], where: str) -> None:
    for key, value in table.items():
        if key not in allowed:
            raise ConfigError(
                f"{where}: unknown key {key!r}. Allowed: {', '.join(sorted(allowed))}"
            )
        if not isinstance(value, allowed[key]) or (
            isinstance(value, bool) and allowed[key] is not bool
        ):
            raise ConfigError(f"{where}: {key!r} has the wrong type")


def parse(text: str, where: str = FILENAME) -> Plan:
    try:
        data: dict[str, Any] = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{where}: not valid TOML ({error})") from error
    _check(data, _TOP, where)
    if data.get("cloud") not in (None, "gcp"):
        raise ConfigError(f'{where}: cloud must be "gcp"')
    chosen = [key for key in ("cloud", "ssh") if data.get(key)] + (
        ["local"] if data.get("local") else []
    )
    if len(chosen) > 1:
        raise ConfigError(f"{where}: choose one of cloud, ssh or local, not {' and '.join(chosen)}")
    servers: list[ServerPlan] = []
    for number, raw in enumerate(data.pop("servers", []), 1):
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: server {number} must be a table")
        table: dict[str, Any] = dict(raw)  # type: ignore[arg-type]
        _check(table, _SERVER, f"{where}: server {number}")
        name = table.pop("preset", None) or table.pop("id", None)
        if not name:
            raise ConfigError(f"{where}: server {number} needs a preset or an id")
        if table.get("credential") not in (None, "none", "env_token", "oauth"):
            raise ConfigError(
                f"{where}: server {number}: credential must be none, env_token or oauth"
            )
        servers.append(ServerPlan(server=str(name), **table))
    return Plan(servers=servers, **data)


def load(path: Path) -> Plan:
    return parse(path.read_text(encoding="utf-8"), str(path))


def discover(explicit: str | None, directory: Path | None = None) -> tuple[Plan, Path | None]:
    """The plan from --config, or from muse-vm.toml in the current folder, or an empty one."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"{explicit}: no such file")
        return load(path), path
    found = (directory or Path.cwd()) / FILENAME
    return (load(found), found) if found.is_file() else (Plan(), None)


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_scalar(item) for item in cast(list[Any], value)) + "]"
    return _quote(str(value))


def dump(plan: Plan) -> str:
    """The plan as TOML (secrets are never in a plan: only the paths of files that hold them)."""
    lines = ["# Written by `muse-vm setup`. Edit it, then: muse-vm setup --config muse-vm.toml"]
    for key in _TOP:
        value = getattr(plan, key, None)
        if key == "servers" or value in (None, [], False):
            continue
        lines.append(f"{key} = {_scalar(value)}")
    for server in plan.servers:
        key = "id" if server.endpoint else "preset"
        lines += ["", "[[servers]]", f"{key} = {_quote(server.server)}"]
        for key in _SERVER:
            if key in ("preset", "id"):
                continue
            value = getattr(server, key, None)
            if value not in (None, [], False):
                lines.append(f"{key} = {_scalar(value)}")
    return "\n".join(lines) + "\n"
