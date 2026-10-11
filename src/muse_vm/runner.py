"""Running commands on this computer.

Everything that touches the outside world goes through a Runner, so tests can swap in one that
records commands and returns canned answers. Secrets are passed on stdin and never in the argument
list, which is visible in the process table and is printed in errors and in verbose mode.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandError(RuntimeError):
    def __init__(self, argv: list[str], result: Result) -> None:
        self.argv, self.result = argv, result
        tail = (result.stderr or result.stdout).strip().splitlines()[-8:]
        super().__init__(
            f"command failed ({result.returncode}): {_short(argv)}\n  " + "\n  ".join(tail)
        )


def _short(argv: list[str], limit: int = 300) -> str:
    text = " ".join(shlex.quote(item) for item in argv)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Runner:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose

    def __call__(
        self,
        argv: list[str],
        input: bytes | str | None = None,
        check: bool = True,
        timeout: float | None = None,
        stream: bool = False,
    ) -> Result:
        if self.verbose:
            print(f"  $ {_short(argv)}", file=sys.stderr)
        data = input.encode() if isinstance(input, str) else input
        # A child must never read the terminal: ssh or docker would swallow keystrokes.
        stdin = subprocess.DEVNULL if data is None else None
        try:
            if stream:
                done = subprocess.run(
                    argv,
                    input=data,
                    stdin=stdin,
                    timeout=timeout,
                    stdout=sys.stderr,
                    stderr=sys.stderr,
                )
                result = Result(done.returncode, "", "")
            else:
                done = subprocess.run(
                    argv, input=data, stdin=stdin, timeout=timeout, capture_output=True
                )
                result = Result(
                    done.returncode,
                    done.stdout.decode(errors="replace"),
                    done.stderr.decode(errors="replace"),
                )
        except FileNotFoundError:
            result = Result(127, "", f"{argv[0]}: command not found")
        except subprocess.TimeoutExpired:
            result = Result(124, "", f"timed out after {timeout}s")
        if check and not result.ok:
            raise CommandError(argv, result)
        return result
