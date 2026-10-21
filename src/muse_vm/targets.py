"""Targets: the machine a deployment lives on.

Every target offers the same few operations (run a shell script in the deployment directory,
write or read a file, run `docker compose`), so the deployment logic is identical for an existing
host over SSH and for a cloud VM reached through a cloud-provided tunnel.
"""

from __future__ import annotations

import base64
import os
import posixpath
import shlex
import time

from .runner import CommandError, Result, Runner

ROOT = "/opt/muse-adapter"

# ssh (and `gcloud compute ssh`, which runs ssh) exits with 255 when the connection itself failed,
# as opposed to the remote command failing. IAP tunnels and busy hosts drop connections now and
# then, so such a failure is retried; every operation here is safe to run again.
CONNECTION_FAILED = 255
RETRY_DELAYS = (3.0, 8.0, 20.0)
_pause = time.sleep


class Target:
    kind = "?"
    retries = True  # a dropped connection is worth retrying (not for commands on this machine)

    def __init__(self, runner: Runner, root: str = ROOT) -> None:
        self.runner, self.root = runner, root

    def _argv(self, script: str) -> list[str]:
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError

    def sh(
        self,
        script: str,
        input: bytes | str | None = None,
        check: bool = True,
        timeout: float | None = 600,
        stream: bool = False,
        cwd_root: bool = True,
        retry: bool = True,
    ) -> Result:
        """Run a POSIX shell script as root, in the deployment directory unless told otherwise.

        A dropped connection is retried unless `retry` is False (a diagnostic should fail fast).
        """
        if cwd_root:
            script = f"cd {shlex.quote(self.root)} && {script}"
        argv = self._argv(script)
        result = Result(CONNECTION_FAILED, "", "")
        for delay in (*RETRY_DELAYS, None) if (retry and self.retries) else (None,):
            result = self.runner(argv, input=input, check=False, timeout=timeout, stream=stream)
            if result.returncode != CONNECTION_FAILED or delay is None:
                break
            _pause(delay)
        if check and not result.ok:
            raise CommandError(argv, result)
        return result

    def put(
        self, relative: str, data: bytes | str, mode: int = 0o644, owner: str | None = None
    ) -> None:
        """Write a file atomically under the deployment directory. The content goes over stdin."""
        path = posixpath.join(self.root, relative)
        temporary = path + ".tmp"
        q = shlex.quote
        chown = f" && chown {q(owner)} {q(temporary)}" if owner else ""
        self.sh(
            f"mkdir -p {q(posixpath.dirname(path))} && (umask 077 && cat > {q(temporary)}) "
            f"&& chmod {mode:o} {q(temporary)}{chown} && mv -f {q(temporary)} {q(path)}",
            input=data,
            cwd_root=False,
        )

    def get(self, relative: str) -> str | None:
        path = posixpath.join(self.root, relative)
        q = shlex.quote
        result = self.sh(
            f"if [ -f {q(path)} ]; then cat {q(path)}; else echo __MISSING__ >&2; exit 3; fi",
            check=False,
            cwd_root=False,
        )
        if result.returncode == 3 and "__MISSING__" in result.stderr:
            return None
        if not result.ok:
            raise CommandError(["get", path], result)
        return result.stdout

    def get_bytes(self, script: str, timeout: float = 600) -> bytes:
        """The standard output of a script, as bytes (carried as base64 so any content survives)."""
        result = self.sh(f"({script}) | base64 | tr -d '\\n'", timeout=timeout)
        return base64.b64decode(result.stdout.strip())

    def put_bytes(self, script: str, data: bytes, timeout: float = 600) -> None:
        """Run a script that reads binary content from its standard input."""
        self.sh(f"base64 -d | ({script})", input=base64.b64encode(data), timeout=timeout)

    def compose(
        self,
        args: str,
        input: bytes | str | None = None,
        check: bool = True,
        timeout: float | None = 900,
        stream: bool = False,
    ) -> Result:
        return self.sh(
            f"docker compose {args}", input=input, check=check, timeout=timeout, stream=stream
        )


class SSHTarget(Target):
    """Any Linux host reachable with ssh: key-based login, and passwordless sudo unless root."""

    kind = "ssh"

    def __init__(
        self, runner: Runner, host: str, options: list[str] | None = None, root: str = ROOT
    ) -> None:
        super().__init__(runner, root)
        self.host, self.options = host, list(options or [])

    def _argv(self, script: str) -> list[str]:
        return [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=20",
            *self.options,
            self.host,
            "sudo -n sh -c " + shlex.quote(script),
        ]

    def describe(self) -> str:
        return f"{self.host}:{self.root}"


class LocalTarget(Target):
    """The machine muse-vm is running on: Linux, as root or through passwordless sudo."""

    kind = "local"
    retries = False

    def _argv(self, script: str) -> list[str]:
        prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
        return [*prefix, "sh", "-c", script]

    def describe(self) -> str:
        return f"this machine:{self.root}"


class GcloudTarget(Target):
    """A Compute Engine VM reached with `gcloud compute ssh` through IAP: no public SSH port."""

    kind = "gcp"

    def __init__(
        self, runner: Runner, instance: str, zone: str, project: str, root: str = ROOT
    ) -> None:
        super().__init__(runner, root)
        self.instance, self.zone, self.project = instance, zone, project

    def _argv(self, script: str) -> list[str]:
        return [
            "gcloud",
            "compute",
            "ssh",
            self.instance,
            f"--zone={self.zone}",
            f"--project={self.project}",
            "--tunnel-through-iap",
            "--quiet",
            "--verbosity=error",  # hides gcloud's NumPy performance warning on every command
            "--command=sudo -n sh -c " + shlex.quote(script),
            "--",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "LogLevel=ERROR",
        ]

    def describe(self) -> str:
        return f"gce://{self.project}/{self.zone}/{self.instance}:{self.root}"
