"""The machine muse-vm is running on: install the adapter right here.

This is what a customer runs on their own VM, with no second computer involved. It is the SSH
provider with the SSH hop removed, so the checks, the install and the removal are the same.
"""

from __future__ import annotations

import sys

from ..targets import LocalTarget, Target
from .ssh import SSHProvider


class LocalProvider(SSHProvider):
    name = "local"

    def target(self) -> Target:
        return LocalTarget(self.runner, self.root)

    def preflight(self) -> None:
        if not sys.platform.startswith("linux"):
            raise SystemExit(
                "--local installs on the machine you are running muse-vm on, which must be "
                "Linux (Debian or Ubuntu). To install on a Linux machine from here, use "
                "--ssh user@host or --cloud gcp."
            )
        super().preflight()
