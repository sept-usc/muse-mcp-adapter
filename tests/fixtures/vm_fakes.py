"""Stand-ins for the machines and commands the VM tooling talks to."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from muse_vm.runner import Result
from muse_vm.targets import Target


class FakeRunner:
    """Records every command and answers from canned results (first matching rule wins)."""

    def __init__(self, rules: list[tuple[str, Result]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.rules = list(rules or [])

    def when(self, fragment: str, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.rules.append((fragment, Result(returncode, stdout, stderr)))

    def __call__(
        self,
        argv: list[str],
        input: bytes | str | None = None,
        check: bool = True,
        timeout: float | None = None,
        stream: bool = False,
    ) -> Result:
        self.calls.append({"argv": list(argv), "input": input, "check": check})
        line = " ".join(argv)
        for fragment, result in self.rules:
            if fragment in line:
                if check and not result.ok:
                    from muse_vm.runner import CommandError

                    raise CommandError(argv, result)
                return result
        return Result(0, "", "")

    def lines(self) -> list[str]:
        return [" ".join(call["argv"]) for call in self.calls]

    def ran(self, fragment: str) -> bool:
        return any(fragment in line for line in self.lines())


@dataclass
class StoredFile:
    data: bytes
    mode: int
    owner: str | None


class FakeTarget(Target):
    """An in-memory machine: files are kept in a dict and scripts are recorded and answered."""

    kind = "fake"

    def __init__(self, runner: FakeRunner | None = None) -> None:
        super().__init__(runner or FakeRunner())  # type: ignore[arg-type]
        self.files: dict[str, StoredFile] = {}
        self.scripts: list[str] = []
        self.answers: list[tuple[str, Result]] = []

    def _argv(self, script: str) -> list[str]:
        return ["fake", script]

    def describe(self) -> str:
        return "fake-target"

    def answer(
        self, fragment: str, stdout: str = "", returncode: int = 0, stderr: str = ""
    ) -> None:
        self.answers.append((fragment, Result(returncode, stdout, stderr)))

    def sh(  # type: ignore[override]
        self,
        script: str,
        input: bytes | str | None = None,
        check: bool = True,
        timeout: float | None = 600,
        stream: bool = False,
        cwd_root: bool = True,
    ) -> Result:
        self.scripts.append(script)
        for fragment, result in self.answers:
            if fragment in script:
                if check and not result.ok:
                    from muse_vm.runner import CommandError

                    raise CommandError(["fake", script], result)
                return result
        return Result(0, "", "")

    def put(
        self, relative: str, data: bytes | str, mode: int = 0o644, owner: str | None = None
    ) -> None:
        raw = data.encode() if isinstance(data, str) else data
        self.files[relative] = StoredFile(raw, mode, owner)

    def get(self, relative: str) -> str | None:
        stored = self.files.get(relative)
        return stored.data.decode() if stored else None

    def text(self, relative: str) -> str:
        return self.files[relative].data.decode()

    def ran(self, fragment: str) -> bool:
        return any(fragment in script for script in self.scripts)


class ContainerTarget(Target):
    """A stand-in for the VM that runs the REAL adapter commands.

    `docker compose exec -T adapter muse-mcp-adapter ...` is turned into a direct call of the
    adapter's command line, with /data and /config mapped to folders on this computer. Everything
    else a deployment would do on the VM is recorded and answered with success.
    """

    kind = "container"

    def __init__(self, root: Any, *, environ: dict[str, str] | None = None) -> None:
        from pathlib import Path

        super().__init__(FakeRunner())  # type: ignore[arg-type]
        self.dir = Path(root)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "data").mkdir(exist_ok=True)
        (self.dir / "config").mkdir(exist_ok=True)
        (self.dir / "secrets").mkdir(exist_ok=True)
        self.environ = environ or {}
        self.scripts: list[str] = []

    def _argv(self, script: str) -> list[str]:
        return ["sh", "-c", script]

    def describe(self) -> str:
        return f"container@{self.dir}"

    # files land in the folder that stands for /opt/muse-adapter
    def put(
        self, relative: str, data: bytes | str, mode: int = 0o644, owner: str | None = None
    ) -> None:
        path = self.dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = data.encode() if isinstance(data, str) else data
        path.write_bytes(raw)
        path.chmod(mode)

    def get(self, relative: str) -> str | None:
        path = self.dir / relative
        return path.read_text() if path.exists() else None

    def sh(  # type: ignore[override]
        self,
        script: str,
        input: bytes | str | None = None,
        check: bool = True,
        timeout: float | None = 600,
        stream: bool = False,
        cwd_root: bool = True,
    ) -> Result:
        import subprocess

        self.scripts.append(script)
        if script.startswith("rm -f data/policies"):
            done = subprocess.run(
                ["sh", "-c", script], cwd=self.dir, capture_output=True, text=True
            )
            return Result(done.returncode, done.stdout, done.stderr)
        return Result(0, "", "")

    def compose(  # type: ignore[override]
        self,
        args: str,
        input: bytes | str | None = None,
        check: bool = True,
        timeout: float | None = 900,
        stream: bool = False,
    ) -> Result:
        import os
        import shlex
        import subprocess
        import sys

        words = shlex.split(args)
        if words[:4] != ["exec", "-T", "adapter", "muse-mcp-adapter"]:
            self.scripts.append(f"compose {args}")
            return Result(0, "", "")
        mapped = [
            word.replace("/data/", f"{self.dir}/data/").replace("/config/", f"{self.dir}/config/")
            for word in words[4:]
        ]
        env = {
            **os.environ,
            **self.environ,
            "MUSE_ADAPTER_SECRETS_DIR": str(self.dir / "secrets"),
            "PYTHONPATH": os.pathsep.join(sys.path),
        }
        done = subprocess.run(
            [sys.executable, "-m", "muse_mcp_adapter", *mapped],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
        result = Result(done.returncode, done.stdout, done.stderr)
        if check and not result.ok:
            from muse_vm.runner import CommandError

            raise CommandError(["muse-mcp-adapter", *words[4:]], result)
        return result
