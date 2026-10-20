"""`muse-vm doctor`: find what would stop a deployment, before anything is created.

Every check says what is wrong in plain words and the one command or click that fixes it.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass

from . import bundle
from .runner import Runner
from .selfcheck import Report
from .targets import SSHTarget

MIN_PYTHON = (3, 12)


@dataclass
class GcpFacts:
    project: str | None = None
    zone: str | None = None
    account: str | None = None
    credentials_ok: bool = True


def gcloud_facts(runner: Runner) -> GcpFacts | None:
    """What gcloud already knows (None when it is not installed): used for defaults everywhere."""
    if not runner(["gcloud", "--version"], check=False).ok:
        return None

    def value(*args: str) -> str | None:
        done = runner(["gcloud", *args], check=False)
        text = done.stdout.strip()
        return text if done.ok and text and text != "(unset)" else None

    account = value("auth", "list", "--filter=status:ACTIVE", "--format=value(account)")
    return GcpFacts(
        project=value("config", "get-value", "project"),
        zone=value("config", "get-value", "compute/zone"),
        account=account,
        # An account can be listed as active while its stored credentials have expired.
        credentials_ok=account is not None
        and runner(["gcloud", "auth", "print-access-token"], check=False).ok,
    )


def local_checks(report: Report) -> None:
    report.add(
        f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer",
        sys.version_info >= MIN_PYTHON,
        f"this is {sys.version_info.major}.{sys.version_info.minor}",
    )
    for tool, why in (
        ("ssh", "reaching the machine"),
        ("git", "recording which commit is deployed"),
    ):
        report.add(f"{tool} is installed (for {why})", shutil.which(tool) is not None)
    source = bundle.find_build_source()
    if source is None:
        report.add(
            "something to build the images from",
            False,
            "install muse-mcp-adapter from a release, or run muse-vm inside a cloned repository",
        )
        return
    if source.kind == "release":
        report.add("the images are built from the installed release", True, source.revision)
        return
    report.add(
        "the checkout is clean",
        not source.dirty,
        f"commit {source.revision}"
        if not source.dirty
        else "uncommitted changes: commit them, or pass --allow-dirty to up",
        required=not source.dirty,
    )


def gcp_checks(report: Report, runner: Runner, project: str | None, zone: str | None) -> None:
    facts = gcloud_facts(runner)
    if facts is None:
        report.add(
            "the gcloud CLI",
            False,
            "install it from https://cloud.google.com/sdk, then run `gcloud auth login`",
        )
        return
    report.add(
        "gcloud is signed in",
        facts.account is not None,
        facts.account or "run `gcloud auth login`",
    )
    if facts.account:
        report.add(
            "gcloud credentials are valid",
            facts.credentials_ok,
            "" if facts.credentials_ok else "they have expired: run `gcloud auth login`",
        )
    chosen = project or facts.project
    report.add(
        "a Google Cloud project",
        chosen is not None,
        chosen or "pass --project, or run `gcloud config set project <id>`",
    )
    if not chosen or not facts.account or not facts.credentials_ok:
        return
    listed = runner(
        [
            "gcloud",
            "services",
            "list",
            "--enabled",
            f"--project={chosen}",
            "--format=value(config.name)",
        ],
        check=False,
    )
    if not listed.ok:
        lines = listed.stderr.strip().splitlines()
        report.add(
            "the project's enabled APIs can be read",
            False,
            lines[-1][:160] if lines else "",
            required=False,
        )
        return
    enabled = listed.stdout.split()
    for api, why in (
        ("compute.googleapis.com", "creating the VM"),
        ("iap.googleapis.com", "SSH through Identity-Aware Proxy"),
    ):
        fix = f"`muse-vm up --yes` enables it, or: gcloud services enable {api} --project {chosen}"
        # Not required: `up` can enable them, so this only says what will happen.
        report.add(
            f"{api} is enabled (for {why})",
            api in enabled,
            "" if api in enabled else fix,
            required=False,
        )
    report.add("zone", True, zone or facts.zone or "us-central1-a (the default)")


def ssh_checks(report: Report, runner: Runner, host: str, options: list[str]) -> None:
    target = SSHTarget(runner, host, options)
    probe = target.sh("uname -s -m && id -u", cwd_root=False, check=False, timeout=60, retry=False)
    if not probe.ok:
        report.add(
            f"{host} accepts key-based ssh with passwordless sudo",
            False,
            f"`ssh {host} sudo -n true` must work: {(probe.stderr or probe.stdout).strip()[:160]}",
        )
        return
    system, architecture = probe.stdout.split()[:2]
    supported = system == "Linux" and architecture in ("x86_64", "aarch64")
    report.add(f"{host} is reachable with passwordless sudo", True)
    report.add("Linux on x86_64 or arm64", supported, f"{system} {architecture}")
    out = target.sh(
        "curl -fsS -m 10 -o /dev/null https://registry-1.docker.io/v2/ ; "
        "echo docker=$?; curl -fsS -m 10 -o /dev/null https://pypi.org/simple/ ; echo pypi=$?",
        cwd_root=False,
        check=False,
        timeout=60,
    ).stdout
    # Docker Hub answers 401 without credentials when it is reachable, which `curl -f` calls 22.
    reachable_docker = "docker=0" in out or "docker=22" in out
    report.add(
        "the machine can reach Docker Hub (to build the images)",
        reachable_docker,
        "" if reachable_docker else "allow outbound HTTPS, or use --image/--guard-image",
        required=False,
    )
    report.add(
        "the machine can reach PyPI (to build the images)",
        "pypi=0" in out,
        "" if "pypi=0" in out else "allow outbound HTTPS, or use --image/--guard-image",
        required=False,
    )
