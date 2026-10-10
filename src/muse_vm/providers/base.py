"""What every place the adapter can be deployed to has to provide."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from .. import bundle
from ..runner import Runner
from ..state import Deployment
from ..targets import ROOT, Target


class Provider:
    name = "?"
    root = ROOT

    def __init__(
        self,
        dep: Deployment,
        runner: Runner,
        say: Callable[[str], None] = print,
        opts: dict[str, Any] | None = None,
    ) -> None:
        self.dep, self.runner, self.say, self.opts = dep, runner, say, opts or {}

    # ---- lifecycle (override) ---------------------------------------------------------------
    def preflight(self) -> None:
        """Check tools, credentials and the machine before anything is created."""

    def provision(self) -> Target:
        """Create what is needed (cloud resources) and return a target that has Docker."""
        return self.target()

    def target(self) -> Target:
        raise NotImplementedError

    def after_start(self, t: Target) -> None:
        """Provider extras once the adapter runs: idle stop, snapshots, a budget alert."""

    def teardown(self, keep_data: bool = False) -> None:
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        return {}

    def ensure_ready(self) -> None:
        """Stop with a hint when the machine cannot be reached (for example a stopped VM)."""

    # ---- shared by dedicated Linux hosts ----------------------------------------------------
    def ensure_docker(self, t: Target) -> None:
        t.sh(f"mkdir -p {self.root} && chmod 755 {self.root}", cwd_root=False)
        t.put("bootstrap.sh", bundle.bundle_file("bootstrap.sh"), 0o755)
        self.say("  preparing the host: Docker, automatic security updates, log limits…")
        t.sh("sh ./bootstrap.sh", timeout=900, stream=True)

    def start_stack(self, t: Target) -> None:
        bundle.install_units(t, idle_hours=self.opts.get("idle_stop_hours"))
        t.sh("systemctl restart muse-adapter.service", timeout=900)

    def start(self) -> None:
        self.target().sh("systemctl start muse-adapter.service", timeout=900)

    def stop(self) -> None:
        self.target().sh("systemctl stop muse-adapter.service", timeout=300)

    def restart_adapter(self, t: Target) -> None:
        t.compose("up -d --force-recreate --no-deps adapter", stream=True)

    def leave_tailnet(self) -> None:
        """Sign the adapter's Tailscale device out, so it does not linger in the owner's tailnet.

        Best effort: a machine that is already gone or stopped simply cannot be asked.
        """
        # nothing here may stop the removal that follows
        with contextlib.suppress(Exception):
            self.target().compose("exec -T tailscale tailscale logout", check=False, timeout=60)

    def remove_from_host(self, t: Target, keep_data: bool) -> None:
        self.leave_tailnet()
        bundle.remove_units(t)
        images = " ".join(bundle.local_images())
        wipe = (
            "find . -mindepth 1 -maxdepth 1 ! -name data ! -name backups -exec rm -rf {} +"
            if keep_data
            else f"cd / && rm -rf {self.root}"
        )
        t.sh(
            "if [ -f compose.yml ]; then docker compose down -v --remove-orphans; fi; "
            f"docker image rm {images} >/dev/null 2>&1; {wipe}",
            check=False,
            timeout=600,
        )
