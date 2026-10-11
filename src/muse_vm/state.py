"""What this computer remembers about each deployment: ~/.config/muse-vm/deployments/<name>.json.

The file never holds a secret. Tokens, connection codes and server credentials exist only on the
VM, and a connection token is shown once, when it is created.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

NAME = re.compile(r"^[a-z][a-z0-9-]{0,19}$")


def config_home() -> Path:
    if os.environ.get("MUSE_VM_HOME"):
        return Path(os.environ["MUSE_VM_HOME"])
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "muse-vm"


def check_name(name: str) -> str:
    if not NAME.match(name):
        raise SystemExit(
            "A deployment name is 1 to 20 characters: lowercase letters, digits and '-', "
            "starting with a letter."
        )
    return name


@dataclass
class Deployment:
    name: str
    provider: str  # ssh | gcp
    ingress: str  # caddy | tailscale-funnel | cloudflare-tunnel
    hostname: str | None = None
    image: str | None = None
    build: bool = False
    created: float = field(default_factory=time.time)
    status: str = "creating"  # creating | ready | failed
    ssh_host: str | None = None
    ssh_opts: list[str] = field(default_factory=list[str])
    project: str | None = None
    zone: str | None = None
    # Cloud resources, recorded as they are created so a failed `up` can roll back exactly them.
    resources: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    servers: list[str] = field(default_factory=list[str])
    extra: dict[str, Any] = field(default_factory=dict[str, Any])

    @property
    def base_url(self) -> str | None:
        return f"https://{self.hostname}" if self.hostname else None

    @staticmethod
    def path_for(name: str) -> Path:
        return config_home() / "deployments" / f"{name}.json"

    def save(self) -> None:
        path = self.path_for(self.name)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(self), indent=2) + "\n")
        temporary.replace(path)

    def delete(self) -> None:
        self.path_for(self.name).unlink(missing_ok=True)

    def add_resource(self, kind: str, name: str, **info: Any) -> None:
        self.resources.append({"kind": kind, "name": name, **info})
        self.save()

    @classmethod
    def load(cls, name: str) -> Deployment:
        path = cls.path_for(name)
        if not path.exists():
            raise SystemExit(f"There is no deployment named {name!r}. See `muse-vm list`.")
        data = json.loads(path.read_text(encoding="utf-8"))
        known = set(cls.__dataclass_fields__)
        return cls(**{key: value for key, value in data.items() if key in known})

    @classmethod
    def all(cls) -> list[Deployment]:
        directory = config_home() / "deployments"
        if not directory.exists():
            return []
        return [cls.load(path.stem) for path in sorted(directory.glob("*.json"))]
