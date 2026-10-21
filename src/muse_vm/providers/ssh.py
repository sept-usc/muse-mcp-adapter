"""Any existing Linux machine you can reach over SSH."""

from __future__ import annotations

from typing import Any

from ..runner import CommandError
from ..targets import SSHTarget, Target
from .base import Provider


class SSHProvider(Provider):
    name = "ssh"

    @property
    def where(self) -> str:
        return self.dep.ssh_host or "this machine"

    def target(self) -> Target:
        assert self.dep.ssh_host
        return SSHTarget(self.runner, self.dep.ssh_host, self.dep.ssh_opts, self.root)

    def preflight(self) -> None:
        t = self.target()
        try:
            probe = t.sh("uname -s -m && id -u", cwd_root=False, timeout=60)
        except CommandError as error:
            raise SystemExit(
                f"Cannot run commands on {self.where} as root with `sudo -n`.\n"
                f"  It needs key-based SSH and passwordless sudo (or a root login).\n  {error}"
            ) from None
        system, architecture = probe.stdout.split()[:2]
        if system != "Linux" or architecture not in ("x86_64", "aarch64"):
            raise SystemExit(
                f"Unsupported target {system}/{architecture}: needs Linux on x86_64 or aarch64."
            )
        self.dep.extra["arch"] = architecture
        existing = t.sh(f"test -f {self.root}/compose.yml && echo yes || echo no", cwd_root=False)
        if existing.stdout.strip() == "yes" and not self.opts.get("reuse_dir"):
            raise SystemExit(
                f"{self.where} already has a deployment in {self.root}. Remove it first "
                "(`muse-vm down` for that deployment) or pass --force."
            )
        cloud = t.sh(
            "curl -s -m 2 -o /dev/null -w '%{http_code}' -H 'Metadata-Flavor: Google' "
            "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/email "
            "|| true",
            cwd_root=False,
            check=False,
        )
        if cloud.stdout.strip() == "200":
            self.say(
                "  ! This host is a Compute Engine VM with a service account attached. The "
                "egress guard blocks the metadata server for the adapter, but remove the account "
                "if you can."
            )

    def provision(self) -> Target:
        t = self.target()
        self.ensure_docker(t)
        return t

    def teardown(self, keep_data: bool = False) -> None:
        t = self.target()
        self.say(f"  contacting {self.where}…")
        why = self.reachable(t)
        if why:
            raise SystemExit(self._unreachable(why))
        self.remove_from_host(t, keep_data)

    def ensure_ready(self) -> None:
        why = self.reachable(self.target())
        if why:
            raise SystemExit(self._unreachable(why))

    def _unreachable(self, why: str) -> str:
        name = self.dep.name
        return (
            f"Could not reach {self.where} ({why}). It may be powered off, or its address "
            f"changed. Start it and run the command again; if it no longer exists, "
            f"`muse-vm down --name {name} --forget` removes the record."
        )

    def status(self) -> dict[str, Any]:
        return {
            "host": self.where,
            "architecture": self.dep.extra.get("arch", "-"),
        }
