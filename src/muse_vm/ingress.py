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


LOGIN_SERVICE = "tailscale-login"
LOGIN_TIMEOUT = 1800  # half an hour for a person to find the link and click it
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


def enable_link(target: Target) -> str:
    """Tailscale's own one-click link that allows HTTPS and Funnel, or "" when it gives none."""
    result = target.compose(
        "exec -T tailscale tailscale debug localapi POST 'query-feature?feature=funnel'",
        check=False,
        timeout=60,
    )
    try:
        url = str(cast(dict[str, Any], json.loads(result.stdout)).get("URL") or "")
    except (ValueError, AttributeError):
        return ""
    return url if url.startswith("https://") else ""


def enable_message(target: Target, missing: list[str], url: str | None = None) -> str:
    """How to allow HTTPS/Funnel: Tailscale's own one-click link when it gives one."""
    url = enable_link(target) if url is None else url
    if url:
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


def interactive_login(
    target: Target,
    *,
    timeout: float = LOGIN_TIMEOUT,
    say: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    open_link: Callable[[str], bool] | None = None,
) -> None:
    """Sign the machine in to the owner's Tailscale account, with a link that stays valid.

    Runs the one-time login service, shows its link (and opens it when it can), and waits until
    the owner has approved it. The login is stored in the state directory the normal Tailscale
    service uses, so that service only has to reconnect.
    """
    target.compose(f"up -d {LOGIN_SERVICE}", stream=True)
    deadline = time.monotonic() + timeout
    shown = ""
    try:
        while time.monotonic() < deadline:
            logs = target.compose(f"logs --no-color {LOGIN_SERVICE}", check=False)
            text = logs.stdout + logs.stderr
            if "Login complete" in text:
                return
            links = TS_LOGIN.findall(text)
            if links and links[-1] != shown:
                shown = links[-1]
                say(f"  Sign in to Tailscale to give the adapter its address: {shown}")
                say("  Approve it in your browser; the install carries on by itself.")
                if open_link:
                    open_link(shown)
            exited = target.compose(
                f"ps -a --status exited -q {LOGIN_SERVICE}", check=False
            ).stdout.strip()
            if exited:
                # It may have finished in the moment between reading the log and asking whether
                # it exited, so read the log once more before calling that a failure.
                final = target.compose(f"logs --no-color {LOGIN_SERVICE}", check=False)
                text = final.stdout + final.stderr
                if "Login complete" in text:
                    return
                lines = text.strip().splitlines()
                own = [line for line in lines if LOGIN_SERVICE in line] or lines
                tail = "\n    ".join(own[-6:])
                raise SystemExit(f"The Tailscale sign-in stopped before it finished:\n    {tail}")
            sleep(3)
        raise SystemExit(
            "The Tailscale sign-in was not completed in time. Run `muse-vm up` again and open "
            "the link as soon as it appears."
        )
    finally:
        target.compose(f"rm -f -s {LOGIN_SERVICE}", check=False)


def hostname_after_start(
    target: Target,
    *,
    timeout: float = 600,
    say: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    open_link: Callable[[str], bool] | None = None,
) -> str:
    """Learn the public hostname once Tailscale runs and Funnel is on for it.

    The owner may have to sign in, approve the device, or allow HTTPS and Funnel in the tailnet;
    each is explained once, and the wait is extended while they do it. When `open_link` is given
    it is called with each link the owner has to click, so their browser can open it for them.
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
                        link = enable_link(target)
                        say(enable_message(target, missing, link))
                        if link and open_link:
                            open_link(link)
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
                    if open_link:
                        open_link(shown_login)
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
