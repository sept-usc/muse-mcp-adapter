"""Run the shipped stack for real and prove it works end to end.

Builds both images, starts the compose file muse-vm deploys (guard + adapter) on a private network,
starts the Atlas validation server as a real MCP server on that network, then drives the same
admin commands `muse-vm add-server` uses and calls the connector over HTTP like Muse does. Checks
the guard's rules, the hardening, authentication, a read, a write exactly once, persistence across a
restart, and backup. Everything is removed afterwards. Needs Docker, the project installed, and
internet access (the guard's self-check confirms the public internet is reachable).
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from muse_vm import bundle, selfcheck
from muse_vm.admin import Admin, pick_exposure
from muse_vm.runner import Result, Runner
from muse_vm.targets import Target

ROOT = Path(__file__).resolve().parents[1]
ADAPTER_IMAGE, GUARD_IMAGE = "mma-stack-adapter:check", "mma-stack-guard:check"
PROJECT, SUBNET, ATLAS_IP = "mmastack", "172.29.77.0/24", "172.29.77.50"
ATLAS_TOKEN = "stack-secret-token"
RUNNER = Runner()
checks: list[str] = []


def sh(*argv: str, check: bool = True, input: str | None = None) -> str:
    done = subprocess.run(argv, capture_output=True, text=True, input=input)
    if check and done.returncode != 0:
        print(f"$ {' '.join(argv)}\n{done.stdout}{done.stderr}")
        sys.exit(1)
    return done.stdout


def expect(condition: bool, what: str, detail: str = "") -> None:
    if not condition:
        print(f"FAILED: {what} {detail}")
        raise SystemExit(1)
    checks.append(what)
    print(f"  ok  {what}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Stack(Target):
    """The deployment directory on this computer, driven exactly like a machine: through compose."""

    kind = "stack"
    retries = False

    def __init__(self, directory: Path) -> None:
        super().__init__(RUNNER, str(directory))

    def _argv(self, script: str) -> list[str]:
        return ["sh", "-c", script]

    def describe(self) -> str:
        return f"stack:{self.root}"

    def put(
        self, relative: str, data: bytes | str, mode: int = 0o644, owner: str | None = None
    ) -> None:
        if relative.startswith("data/"):  # the adapter's volume: write it as the adapter would
            path = "/" + relative
            self.compose(
                f"exec -T adapter sh -c 'mkdir -p \"$(dirname {path})\" && cat > {path}'",
                input=data,
            )
            return
        target = Path(self.root) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data.encode() if isinstance(data, str) else data)
        target.chmod(mode)


def http(method: str, url: str, token: str | None = None, body: Any = None, key: str | None = None):
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    if key:
        headers["idempotency-key"] = key
    request = urllib.request.Request(
        url,
        method=method,
        headers=headers,
        data=None if body is None else json.dumps(body).encode(),
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as reply:
            return reply.status, dict(reply.headers), json.loads(reply.read() or b"null")
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, dict(error.headers), json.loads(raw) if raw else None


def cleanup() -> None:
    """Remove everything this check creates, by name, whether or not an earlier run finished."""
    sh("docker", "rm", "-f", "mma-stack-atlas", check=False)
    for kind in ("container", "volume", "network"):
        listed = sh(
            "docker",
            kind,
            "ls",
            "-a" if kind == "container" else "-q",
            "--filter",
            f"name={PROJECT}",
            "--format",
            "{{.Names}}" if kind == "container" else "{{.Name}}",
            check=False,
        ).split()
        if listed:
            sh("docker", kind, "rm", *(["-f"] if kind != "network" else []), *listed, check=False)


def main() -> None:
    cleanup()
    work = Path(tempfile.mkdtemp(prefix="mma-stack-"))
    port = free_port()
    try:
        run(work, port)
    finally:
        cleanup()
        shutil.rmtree(work, ignore_errors=True)


def run(work: Path, port: int) -> None:
    print("building the images")
    sh("docker", "build", "-q", "-t", ADAPTER_IMAGE, str(ROOT))
    sh("docker", "build", "-q", "-t", GUARD_IMAGE, str(ROOT / "deploy" / "guard"))

    # ---- the deployment directory, as muse-vm lays it out (named volumes instead of host folders,
    # so nothing needs root or a particular owner on the computer running this check)
    (work / "compose.yml").write_text(bundle.bundle_file("compose.yml"))
    (work / "compose.test.yml").write_text(
        f"""services:
  guard:
    ports: ["127.0.0.1:{port}:8080"]
  adapter:
    volumes:
      - ./config:/config:ro
      - data:/data
      - secrets:/run/secrets:ro
      - backups:/backups
networks:
  mma:
    ipam:
      config:
        - subnet: {SUBNET}
volumes:
  data: {{}}
  secrets: {{}}
  backups: {{}}
"""
    )
    env = bundle.compose_env(
        project=PROJECT,
        build=False,
        image=ADAPTER_IMAGE,
        guard_image=GUARD_IMAGE,
        egress_allow=SUBNET,
    )
    env["COMPOSE_FILE"] = "compose.yml:compose.test.yml"
    (work / ".env").write_text(bundle.render_env(env))
    config = json.loads(bundle.serve_config("stack.example.ts.net"))
    # only because the test's MCP server is on a private network over plain HTTP; the configuration
    # muse-vm writes for a real machine sets neither
    config.update(allowPrivateNetworks=True, allowInsecureHttp=True)
    (work / "config").mkdir()
    (work / "config" / "serve.json").write_text(json.dumps(config))
    (work / "config").chmod(0o755)
    (work / "config" / "serve.json").chmod(0o644)
    stack = Stack(work)

    print("starting the stack")
    stack.compose("up -d --wait guard adapter", stream=True, timeout=600)
    sh(
        "docker",
        "run",
        "--rm",
        "--user",
        "0",
        "-v",
        f"{PROJECT}_secrets:/s",
        "--entrypoint",
        "sh",
        ADAPTER_IMAGE,
        "-c",
        f"printf '%s\\n' {ATLAS_TOKEN} > /s/ATLAS_TOKEN && chmod 444 /s/ATLAS_TOKEN",
    )
    # a new volume is owned by root; the install gives the backups folder to the adapter user
    sh(
        "docker",
        "run",
        "--rm",
        "--user",
        "0",
        "-v",
        f"{PROJECT}_backups:/b",
        "--entrypoint",
        "sh",
        ADAPTER_IMAGE,
        "-c",
        "chown 10001:10001 /b && chmod 700 /b",
    )
    sh(
        "docker",
        "run",
        "-d",
        "--name",
        "mma-stack-atlas",
        "--network",
        f"{PROJECT}_mma",
        "--ip",
        ATLAS_IP,
        "-v",
        f"{ROOT / 'examples'}:/ex:ro",
        "-e",
        "PYTHONPATH=/ex",
        "--entrypoint",
        "python",
        ADAPTER_IMAGE,
        "-m",
        "validation_server",
        "--host",
        ATLAS_IP,
        "--port",
        "9101",
        "--auth",
        "bearer",
        "--token",
        ATLAS_TOKEN,
    )
    ready = f"import urllib.request;urllib.request.urlopen('http://{ATLAS_IP}:9101/_control/state',timeout=3)"
    for _ in range(40):
        if stack.compose(f'exec -T adapter python -c "{ready}"', check=False).ok:
            break
        subprocess.run(["sleep", "1"])
    else:
        print(sh("docker", "logs", "mma-stack-atlas", check=False))
        raise SystemExit("the test MCP server did not start")
    expect(True, "the adapter reaches an MCP server on the one private range it was allowed")

    print("hardening, as deployed")
    adapter = json.loads(sh("docker", "inspect", f"{PROJECT}-adapter-1"))[0]
    guard = json.loads(sh("docker", "inspect", f"{PROJECT}-guard-1"))[0]
    expect(adapter["HostConfig"]["ReadonlyRootfs"] is True, "the adapter's filesystem is read-only")
    expect(adapter["Config"]["User"] == "10001:10001", "the adapter runs as an unprivileged user")
    expect(
        adapter["HostConfig"]["CapDrop"] == ["ALL"] and not adapter["HostConfig"]["CapAdd"],
        "the adapter has no capabilities",
    )
    added = [item.removeprefix("CAP_") for item in guard["HostConfig"]["CapAdd"] or []]
    expect(
        added == ["NET_ADMIN"] and guard["HostConfig"]["ReadonlyRootfs"],
        "the guard has only NET_ADMIN and a read-only filesystem",
    )
    expect(
        not adapter["HostConfig"]["Privileged"] and not guard["HostConfig"]["Privileged"],
        "nothing is privileged",
    )

    print("the self-check muse-vm runs after every deployment")
    report = selfcheck.run(stack, f"http://127.0.0.1:{port}", wait=60, say=lambda _line: None)
    for line in report.lines():
        print(line)
    # The one check about host folders (modes 700) cannot apply: this test uses Docker volumes so it
    # needs no particular owner on this computer. The same fact is checked inside the container.
    others = [
        ok
        for name, ok, _, required in report.checks
        if required and not name.startswith("data and secrets")
    ]
    expect(all(others), "every self-check passes (guard rules, SSRF canary, headers, 401s)")
    mode = stack.compose("exec -T adapter stat -c '%a %u' /data").stdout.strip()
    expect(
        mode == "700 10001",
        "the adapter's data directory is private and owned by the adapter",
        mode,
    )

    print("connecting a server and using it, as Muse does")
    admin = Admin(stack, actor="stack-check", dev_network=True)
    endpoint = f"http://{ATLAS_IP}:9101/mcp"
    admin.register("atlas", "Atlas", endpoint, "bearer")
    found = admin.discover("atlas", token_env="ATLAS_TOKEN")
    expect(found > 0, "discovery finds the server's tools", f"({found})")
    policies = admin.draft_policy("atlas", expose_reads=True)
    exposed = pick_exposure(policies, allow=["create_issue"])
    expect(
        any("create_issue" in item for item in exposed), "a write is exposed only when asked for"
    )
    admin.publish("atlas", "1.0.0", policies)
    linked = admin.link("atlas", "Atlas", "env_token", "ATLAS_TOKEN")
    slug = re.search(r"/c/([^/]+)/", linked.connector_url).group(1)  # type: ignore[union-attr]
    base = f"http://127.0.0.1:{port}/c/{slug}"

    status, _, spec = http("GET", f"{base}/openapi.json")
    expect(
        status == 200 and "/operations/get_issue" in spec["paths"],
        "the API description lists the tools",
    )
    status, _, _ = http("POST", f"{base}/operations/get_issue", body={"issue_id": "ISSUE-1"})
    expect(status == 401, "a call without a token is refused")
    status, _, answer = http(
        "POST", f"{base}/operations/get_issue", linked.token, {"issue_id": "ISSUE-1"}
    )
    expect(
        status == 200 and answer["operationStatus"] == "COMPLETED",
        "a read through the adapter works",
    )
    status, _, other = http("POST", f"{base}/operations/get_issue", linked.token, {"issue_id": 7})
    expect(status == 400, "invalid input is refused before it reaches the server")

    first = http(
        "POST", f"{base}/operations/create_issue", linked.token, {"title": "once"}, key="k1"
    )
    second = http(
        "POST", f"{base}/operations/create_issue", linked.token, {"title": "once"}, key="k1"
    )
    expect(
        first[0] == second[0] == 200 and first[2]["operationId"] == second[2]["operationId"],
        "a repeated write with the same key returns the same operation",
    )
    probe = (
        "import json,urllib.request;"
        f"print(json.dumps(json.load(urllib.request.urlopen('http://{ATLAS_IP}:9101/_control/ledger'))))"
    )
    ledger = json.loads(stack.compose(f'exec -T adapter python -c "{probe}"').stdout)
    writes = [item for item in ledger if item["tool"] == "create_issue"]
    expect(len(writes) == 1, "the write really happened exactly once", f"(ledger: {len(writes)})")

    print("persistence and backup")
    stack.compose("restart adapter", stream=True)
    stack.compose("up -d --wait adapter", stream=True)
    status, _, again = http(
        "POST", f"{base}/operations/get_issue", linked.token, {"issue_id": "ISSUE-1"}
    )
    expect(status == 200, "the connection survives a restart (state is on the volume)")
    made = stack.compose(
        "exec -T adapter muse-mcp-adapter --database /data/adapter.db --config /config/serve.json "
        "backup --to /backups"
    )
    expect(
        made.ok and "MANIFEST" in stack.compose("exec -T adapter ls -R /backups").stdout,
        "a backup is written with its manifest",
    )
    leaked = stack.compose("logs --no-color adapter guard").stdout
    expect(linked.token not in leaked and ATLAS_TOKEN not in leaked, "no token appears in the logs")

    print(f"\nthe shipped stack works end to end ({len(checks)} checks)")


main()
