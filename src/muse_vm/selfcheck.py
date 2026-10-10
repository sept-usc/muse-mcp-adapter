"""The check at the end of `muse-vm up`: the deployment from outside, its hardening from inside."""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

from .targets import Target

CANARY = r"""
import errno, json, socket
result = {}
for host in ["169.254.169.254", "10.0.0.1", "172.16.0.1", "192.168.0.1", "100.64.0.1"]:
    sock = socket.socket()
    sock.settimeout(3)
    try:
        sock.connect((host, 80))
        result[host] = "CONNECTED"
    except socket.timeout:
        result[host] = "timeout"
    except OSError as error:
        result[host] = errno.errorcode.get(error.errno, str(error))
    finally:
        sock.close()
try:
    socket.create_connection(("1.1.1.1", 443), 5).close()
    result["public"] = "ok"
except OSError as error:
    result["public"] = str(error)
print(json.dumps(result))
"""


@dataclass
class Report:
    checks: list[tuple[str, bool, str, bool]] = field(
        default_factory=list[tuple[str, bool, str, bool]]
    )

    def add(self, name: str, ok: bool, detail: str = "", required: bool = True) -> None:
        self.checks.append((name, ok, detail, required))

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _, required in self.checks if required)

    def lines(self) -> list[str]:
        def mark(ok: bool, required: bool) -> str:
            return "✓" if ok else ("✗" if required else "!")

        return [
            f"  {mark(ok, required)} {name}{' (' + detail + ')' if detail else ''}"
            for name, ok, detail, required in self.checks
        ]


DOH_ENDPOINTS = (
    "https://1.1.1.1/dns-query?name={name}&type=A",
    "https://8.8.8.8/resolve?name={name}&type=A",
)


def _is_global(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


def public_address(host: str) -> str | None:
    """A public IPv4 address for host when this computer's own DNS cannot give one.

    A computer that is itself in the owner's tailnet answers a Funnel name with its private
    100.x address, and a brand-new name may not have reached this computer's resolver yet. Muse
    uses public DNS, so asking a public resolver over HTTPS shows what Muse will see. Returns
    None when the system resolver already answers with a public address (the normal case).
    """
    found: set[str] = set()
    try:
        for item in socket.getaddrinfo(host, 443, socket.AF_INET):
            found.add(str(item[4][0]))
    except OSError:
        pass
    if any(_is_global(address) for address in found):
        return None
    for endpoint in DOH_ENDPOINTS:
        request = urllib.request.Request(
            endpoint.format(name=urllib.parse.quote(host)),
            headers={"accept": "application/dns-json", "user-agent": "muse-vm-selfcheck"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                answers = json.loads(response.read()).get("Answer", [])
        except (OSError, ValueError):
            continue
        for answer in answers:
            data = str(answer.get("data", ""))
            if answer.get("type") == 1 and _is_global(data):
                return data
    return None


class _PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS to a given IP address, verifying the certificate against the real hostname."""

    def __init__(self, host: str, address: str, timeout: float) -> None:
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]


def _request_pinned(
    method: str,
    url: str,
    address: str,
    body: bytes | None,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, dict[str, str], str]:
    parts = urllib.parse.urlsplit(url)
    connection = _PinnedHTTPS(str(parts.hostname), address, timeout)
    try:
        connection.request(
            method, parts.path or "/", body=body, headers={"host": str(parts.hostname), **headers}
        )
        response = connection.getresponse()
        return (
            response.status,
            {k.lower(): v for k, v in response.getheaders()},
            response.read().decode(errors="replace"),
        )
    finally:
        connection.close()


def _request(
    method: str,
    url: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15,
) -> tuple[int, dict[str, str], str]:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        address = public_address(parts.hostname)
        if address:
            return _request_pinned(
                method,
                url,
                address,
                body,
                {
                    "content-type": "application/json",
                    "user-agent": "muse-vm-selfcheck",
                    **(headers or {}),
                },
                timeout,
            )
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "content-type": "application/json",
            "user-agent": "muse-vm-selfcheck",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (
                response.status,
                {k.lower(): v for k, v in response.headers.items()},
                response.read().decode(errors="replace"),
            )
    except urllib.error.HTTPError as error:
        return (
            error.code,
            {k.lower(): v for k, v in error.headers.items()},
            error.read().decode(errors="replace"),
        )


def wait_healthy(
    base_url: str,
    timeout: float = 300,
    say: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, str]:
    """Wait for https://host/health: a certificate or a tunnel can take minutes to be ready."""
    deadline = time.monotonic() + timeout
    last, said = "", False
    while time.monotonic() < deadline:
        try:
            status, _, body = _request("GET", base_url + "/health", timeout=10)
            if status == 200 and '"ok"' in body:
                return True, ""
            last = f"HTTP {status}"
        except (OSError, ValueError) as error:  # DNS not ready, TLS not ready, connection refused
            last = f"{type(error).__name__}: {error}"
        if not said:
            say(f"  waiting for {base_url}/health (certificate or tunnel)…")
            said = True
        sleep(5)
    return False, last


def _status(method: str, url: str, **kwargs: object) -> tuple[int | None, str]:
    for attempt in range(4):  # a tunnel drops a connection now and then; retry before it counts
        try:
            status, _, _ = _request(method, url, **kwargs)  # type: ignore[arg-type]
            return status, f"HTTP {status}"
        except OSError as error:
            if attempt == 3:
                return None, f"{type(error).__name__}: {error}"
            time.sleep(3)
    return None, "unreachable"


def run(
    target: Target,
    base_url: str,
    *,
    wait: float = 300,
    say: Callable[[str], None] = print,
) -> Report:
    report = Report()
    healthy, why = wait_healthy(base_url, wait, say)
    report.add("public HTTPS /health answers", healthy, why)
    if not healthy:
        # Still look inside: it tells a broken deployment from an address that is not reachable.
        report.checks.extend(inside_checks(target).checks)
        return report

    _, headers, _ = _request("GET", base_url + "/health")
    report.add(
        "security headers (HSTS, nosniff, no-store)",
        "strict-transport-security" in headers
        and headers.get("x-content-type-options") == "nosniff"
        and headers.get("cache-control") == "no-store",
    )
    code, detail = _status("POST", base_url + "/c/selfcheck/operations/none", body=b"{}")
    report.add("a call without a token is refused", code == 401, detail)
    code, detail = _status(
        "POST",
        base_url + "/c/selfcheck/operations/none",
        body=b"{}",
        headers={"authorization": "Bearer mcpa_not_a_token"},
    )
    report.add("a call with a wrong token is refused", code == 401, detail)
    code, detail = _status("GET", base_url + "/c/doesnotexist/openapi.json")
    report.add("an unknown connector has no description", code in (401, 404), detail)

    report.checks.extend(inside_checks(target).checks)
    return report


def inside_checks(target: Target) -> Report:
    report = Report()
    guard = target.compose("exec -T guard nft list table inet mma_guard", check=False)
    report.add(
        "egress guard rules installed",
        guard.ok and "block4" in guard.stdout,
        "" if guard.ok else guard.stderr.strip()[:200],
    )
    canary = target.compose("exec -T adapter python -", input=CANARY, check=False, timeout=120)
    try:
        found = json.loads(canary.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        report.add(
            "network canary ran inside the adapter",
            False,
            (canary.stderr or canary.stdout).strip()[:200],
        )
    else:
        public = found.pop("public", "")
        leaked = [host for host, outcome in found.items() if outcome == "CONNECTED"]
        report.add(
            "metadata and private ranges unreachable from the adapter",
            not leaked,
            ("reachable: " + ", ".join(leaked))
            if leaked
            else ", ".join(f"{h} {v}" for h, v in found.items()),
        )
        report.add(
            "the public internet is reachable from the adapter",
            public == "ok",
            "" if public == "ok" else public,
            required=False,
        )
    user = target.compose("exec -T adapter id -u", check=False)
    report.add(
        "the adapter runs as an unprivileged user",
        user.ok and user.stdout.strip() == "10001",
        user.stdout.strip(),
    )
    writable = target.compose(
        "exec -T adapter sh -c 'touch /probe 2>/dev/null && echo writable || echo read-only'",
        check=False,
    )
    report.add(
        "the adapter's filesystem is read-only",
        "read-only" in writable.stdout,
        writable.stdout.strip(),
    )
    modes = target.sh("stat -c '%a' data secrets", check=False)
    report.add(
        "data and secrets are private (mode 700)",
        modes.ok and set(modes.stdout.split()) == {"700"},
        " ".join(modes.stdout.split()),
    )
    return report
