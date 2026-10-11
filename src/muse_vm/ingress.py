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


TS_ADMIN = "https://login.tailscale.com/admin"
FUNNEL_CAPS = ("https", "funnel")
# A first-time name can take minutes to reach public DNS, and a person may need to approve a
# setting in the Tailscale console, so waiting is cheap and giving up early is not.
EXTENSION = 900


def missing_capabilities(status: dict[str, Any]) -> list[str]:
    """What the owner's tailnet has not yet allowed this device (HTTPS certificates, Funnel)."""
    me = cast(dict[str, Any], status.get("Self") or {})
    have = set(cast(dict[str, Any], me.get("CapMap") or {})) | set(
        cast(list[str], me.get("Capabilities") or [])
    )
    return [capability for capability in FUNNEL_CAPS if capability not in have]


def enable_message(target: Target, missing: list[str]) -> str:
    """How to allow HTTPS/Funnel: Tailscale's own one-click link when it gives one."""
    result = target.compose(
        "exec -T tailscale tailscale debug localapi POST 'query-feature?feature=funnel'",
        check=False,
        timeout=60,
    )
    try:
        url = str(cast(dict[str, Any], json.loads(result.stdout)).get("URL") or "")
    except (ValueError, AttributeError):
        url = ""
    if url.startswith("https://"):
        return (
            "  Tailscale: your tailnet does not allow HTTPS and Funnel yet (a one-time setting).\n"
            f"    Open {url}\n  and approve it; waiting…"
        )
    steps: list[str] = []
    if "https" in missing:
        steps.append(f"turn on HTTPS certificates: {TS_ADMIN}/dns, then Enable HTTPS")
    if "funnel" in missing:
        steps.append(
            f"allow Funnel in the access controls ({TS_ADMIN}/acls) by adding "
            '"nodeAttrs": [{"target": ["autogroup:member"], "attr": ["funnel"]}]'
        )
    listed = "".join(f"    {n}. {step}\n" for n, step in enumerate(steps, 1))
    return (
        "  Tailscale: your tailnet does not allow HTTPS and Funnel yet (one-time):\n"
        f"{listed}  waiting…"
    )


def funnel_published(target: Target, dns: str) -> bool:
    result = target.compose(
        "exec -T tailscale tailscale serve status --json", check=False, timeout=60
    )
    try:
        config = cast(dict[str, Any], json.loads(result.stdout or "{}") or {})
    except ValueError:
        return False
    return bool(cast(dict[str, Any], config.get("AllowFunnel") or {}).get(f"{dns}:443"))


def ensure_certificate(
    target: Target, dns: str, say: Callable[[str], None] = print, attempts: int = 3
) -> bool:
    """Fetch the HTTPS certificate now instead of during the first visitor's TLS handshake.

    Otherwise a name without a cached certificate (a new or renamed device) is certified while
    the handshake waits, the request can outlast the handshake's deadline every time, and the
    public address never answers.
    """
    last = ""
    for attempt in range(attempts):
        result = target.compose(
            f"exec -T tailscale sh -c 'tailscale cert --cert-file - --key-file - {dns} >/dev/null'",
            check=False,
            timeout=180,
        )
        if result.ok:
            return True
        last = ((result.stderr or result.stdout).strip().splitlines() or ["?"])[-1]
        if attempt + 1 < attempts:
            time.sleep(5)
    say(f"  ! no HTTPS certificate for {dns} yet: {last}")
    return False


def hostname_after_start(
    target: Target,
    *,
    timeout: float = 600,
    say: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Learn the public hostname once Tailscale runs and Funnel is on for it.

    The owner may have to sign in, approve the device, or allow HTTPS and Funnel in the tailnet;
    each is explained once, and the wait is extended while they do it.
    """
    deadline = time.monotonic() + timeout
    shown_login = ""
    shown_enable = shown_approval = False
    while time.monotonic() < deadline:
        status = tailscale_status(target)
        if status:
            state = status.get("BackendState")
            me = cast(dict[str, Any], status.get("Self") or {})
            name = str(me.get("DNSName") or "").rstrip(".")
            if state == "Running" and name:
                missing = missing_capabilities(status)
                if missing:
                    if not shown_enable:
                        say(enable_message(target, missing))
                        shown_enable = True
                        deadline = max(deadline, time.monotonic() + EXTENSION)
                elif funnel_published(target, name):
                    ensure_certificate(target, name, say)
                    return name
            elif state == "NeedsMachineAuth" and not shown_approval:
                say(
                    "  Tailscale: this tailnet requires device approval. Approve the new device "
                    f"at {TS_ADMIN}/machines; waiting…"
                )
                shown_approval = True
                deadline = max(deadline, time.monotonic() + EXTENSION)
            elif state == "NeedsLogin":
                match = TS_LOGIN.search(ingress_logs(target))
                if match and match.group(0) != shown_login:
                    shown_login = match.group(0)
                    say(f"  Sign in to Tailscale to give the adapter its address: {shown_login}")
                    deadline = max(deadline, time.monotonic() + EXTENSION)
        sleep(5)
    if shown_enable:
        raise SystemExit(
            "HTTPS and Funnel were not allowed for the tailnet in time. Allow them as described "
            "above, then run `muse-vm up` again."
        )
    raise SystemExit(
        "The Tailscale address did not appear. Sign in with the link above, make sure HTTPS and "
        "Funnel are enabled for your tailnet, then run `muse-vm up` again."
    )
