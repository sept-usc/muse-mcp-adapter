"""Ingress: how Muse, which runs in Meta's cloud, reaches the adapter over public HTTPS."""

from __future__ import annotations

import ipaddress
import json
import re
import time
from collections.abc import Callable
from typing import Any, cast

from .bundle import ingress_logs
from .targets import Target

TS_LOGIN = re.compile(r"https://login\.tailscale\.com/a/[A-Za-z0-9]+")
STABLE = {"caddy": True, "tailscale-funnel": True, "cloudflare-tunnel": True}


def is_public_ip(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


def sslip_name(address: str) -> str:
    """A hostname that resolves to the address and can get a certificate, with no domain needed."""
    parsed = ipaddress.ip_address(address)
    if parsed.version == 4:
        return address.replace(".", "-") + ".sslip.io"
    return parsed.exploded.replace(":", "-") + ".sslip.io"


def hostname_before_start(ingress: str, *, domain: str | None, public_ip: str | None) -> str | None:
    """The ingresses whose hostname is known before anything starts."""
    if ingress == "caddy":
        if domain:
            return domain
        if not public_ip or not is_public_ip(public_ip):
            raise SystemExit(
                "The caddy ingress needs a public IP address (or --domain pointing at this host). "
                "Use --ingress tailscale-funnel or cloudflare-tunnel behind NAT."
            )
        return sslip_name(public_ip)
    if ingress == "cloudflare-tunnel":
        if not domain:
            raise SystemExit(
                "--ingress cloudflare-tunnel needs --domain: the public hostname you mapped to "
                "http://adapter:8080 in the Cloudflare dashboard."
            )
        return domain
    return None


def tailscale_status(target: Target) -> dict[str, Any] | None:
    result = target.compose("exec -T tailscale tailscale status --json", check=False, timeout=60)
    if not result.ok:
        return None
    try:
        return cast(dict[str, Any], json.loads(result.stdout))
    except ValueError:
        return None


def hostname_after_start(
    target: Target,
    ingress: str,
    *,
    timeout: float = 600,
    say: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Learn a tunnel ingress's hostname once its container runs; a login may be needed first."""
    if ingress != "tailscale-funnel":
        raise SystemExit(f"The {ingress} ingress needs a hostname up front.")
    deadline = time.monotonic() + timeout
    shown = ""
    while time.monotonic() < deadline:
        status = tailscale_status(target)
        if status:
            me = cast(dict[str, Any], status.get("Self") or {})
            name = str(me.get("DNSName") or "").rstrip(".")
            if status.get("BackendState") == "Running" and name:
                return name
            if status.get("BackendState") == "NeedsLogin":
                match = TS_LOGIN.search(ingress_logs(target, ingress))
                if match and match.group(0) != shown:
                    shown = match.group(0)
                    say(f"  Sign in to Tailscale to give the adapter its address: {shown}")
        sleep(5)
    raise SystemExit(
        "The Tailscale address did not appear. Sign in with the link above, make sure HTTPS and "
        "Funnel are enabled for your tailnet, then run `muse-vm up` again."
    )
