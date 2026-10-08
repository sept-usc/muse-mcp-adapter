"""Any existing Linux machine you can reach over SSH."""

from __future__ import annotations

import socket
from typing import Any

from ..ingress import is_public_ip
from ..runner import CommandError
from ..targets import SSHTarget, Target
from .base import Provider


class SSHProvider(Provider):
    name = "ssh"

    def target(self) -> Target:
        assert self.dep.ssh_host
        return SSHTarget(self.runner, self.dep.ssh_host, self.dep.ssh_opts, self.root)

    def _resolve_ip(self) -> str | None:
        if self.dep.extra.get("public_ip"):
            return str(self.dep.extra["public_ip"])
        assert self.dep.ssh_host
        host = self.dep.ssh_host.rsplit("@", 1)[-1]
        described = self.runner(["ssh", "-G", *self.dep.ssh_opts, self.dep.ssh_host], check=False)
        for line in described.stdout.splitlines():
            if line.startswith("hostname "):
                host = line.split(" ", 1)[1].strip()
        try:
            address = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
        except OSError:
            return None
        self.dep.extra["public_ip"] = str(address)
        return str(address)

    def public_ip(self) -> str | None:
        return self._resolve_ip()

    def default_ingress(self) -> str:
        address = self._resolve_ip()
        public = bool(address and is_public_ip(address)) or bool(self.opts.get("domain"))
        return "caddy" if public else "tailscale-funnel"

    def preflight(self) -> None:
        t = self.target()
        try:
            probe = t.sh("uname -s -m && id -u", cwd_root=False, timeout=60)
        except CommandError as error:
            raise SystemExit(
                f"Cannot run commands on {self.dep.ssh_host} as root with `sudo -n`.\n"
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
                f"{self.dep.ssh_host} already has a deployment in {self.root}. Remove it first "
                "(`muse-vm down` for that deployment) or pass --force."
            )
        if self.dep.ingress == "caddy":
            busy = t.sh(
                "ss -ltnH 2>/dev/null | awk '{print $4}' | grep -E ':(80|443)$' || true",
                cwd_root=False,
            )
            if busy.stdout.strip():
                raise SystemExit(
                    f"Ports 80 and 443 are already in use on {self.dep.ssh_host} "
                    f"({' '.join(busy.stdout.split())}). Use a tunnel ingress instead."
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
        self.remove_from_host(self.target(), keep_data)

    def status(self) -> dict[str, Any]:
        return {
            "host": self.dep.ssh_host,
            "public IP": self.dep.extra.get("public_ip") or "-",
            "architecture": self.dep.extra.get("arch", "-"),
        }
