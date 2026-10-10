"""Ingress: Muse, in the cloud, reaches the adapter over public HTTPS through Tailscale Funnel."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any, cast

from .bundle import ingress_logs
from .targets import Target

TS_LOGIN = re.compile(r"https://login\.tailscale\.com/a/[A-Za-z0-9]+")


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
    *,
    timeout: float = 600,
    say: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Learn the public hostname once Tailscale runs; the owner may need to sign in first."""
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
                match = TS_LOGIN.search(ingress_logs(target))
                if match and match.group(0) != shown:
                    shown = match.group(0)
                    say(f"  Sign in to Tailscale to give the adapter its address: {shown}")
        sleep(5)
    raise SystemExit(
        "The Tailscale address did not appear. Sign in with the link above, make sure HTTPS and "
        "Funnel are enabled for your tailnet, then run `muse-vm up` again."
    )
