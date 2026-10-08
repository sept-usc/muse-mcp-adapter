"""Google Cloud: create a hardened VM in your own project.

It uses your own `gcloud` login and stores nothing. Every resource is named `mma-<name>` and
recorded in the deployment state the moment it exists, so a failed `up` rolls back exactly what it
made and `down` removes exactly that.

Unlike the project's `default` network, which ships with firewall rules that allow SSH and RDP from
anywhere, the VM lives in a network of its own that has no rules except the ones created here:
SSH only from Google's IAP range, and HTTPS only when the Caddy ingress needs it.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from typing import Any

from .. import bundle
from ..runner import CommandError, Result
from ..targets import GcloudTarget, Target
from .base import Provider

IAP_RANGE = "35.235.240.0/20"
SUBNET_RANGE = "10.20.0.0/24"
ARM_PREFIXES = ("t2a", "c4a")
MONTHLY_USD = {"e2-micro": 7, "e2-small": 13, "e2-medium": 25}
# What is deleted first when a deployment is removed: the VM before the things it uses.
TEARDOWN_ORDER = [
    "instance",
    "snapshot-schedule",
    "budget",
    "router",
    "firewall",
    "address",
    "subnet",
    "network",
]


class GcpProvider(Provider):
    name = "gcp"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        dep = self.dep
        dep.project = dep.project or self.opts.get("project")
        dep.zone = dep.zone or self.opts.get("zone")
        dep.extra.setdefault("machine_type", self.opts.get("machine_type") or "e2-small")

    # ---- names -------------------------------------------------------------------------------
    @property
    def instance(self) -> str:
        return f"mma-{self.dep.name}"

    @property
    def region(self) -> str:
        assert self.dep.zone
        return self.dep.zone.rsplit("-", 1)[0]

    @property
    def private(self) -> bool:
        """No public address: only tunnels need no inbound connection."""
        return self.dep.ingress != "caddy"

    def _g(self, *args: str, check: bool = True, timeout: float = 600) -> Result:
        return self.runner(
            ["gcloud", *args, f"--project={self.dep.project}", "--quiet"],
            check=check,
            timeout=timeout,
        )

    def target(self) -> Target:
        assert self.dep.zone and self.dep.project
        return GcloudTarget(self.runner, self.instance, self.dep.zone, self.dep.project, self.root)

    def public_ip(self) -> str | None:
        address = self.dep.extra.get("public_ip")
        return str(address) if address else None

    # ---- preflight ---------------------------------------------------------------------------
    def preflight(self) -> None:
        if not self.runner(["gcloud", "--version"], check=False).ok:
            raise SystemExit(
                "The gcloud CLI is required for --cloud gcp (https://cloud.google.com/sdk). "
                "Run `gcloud auth login` first."
            )
        dep = self.dep
        if not dep.project:
            configured = self.runner(["gcloud", "config", "get-value", "project"], check=False)
            dep.project = configured.stdout.strip() or None
        if not dep.project or dep.project == "(unset)":
            raise SystemExit(
                "No GCP project: pass --project or run `gcloud config set project <id>`."
            )
        if not dep.zone:
            zone = self.runner(
                ["gcloud", "config", "get-value", "compute/zone"], check=False
            ).stdout.strip()
            dep.zone = zone if zone and zone != "(unset)" else "us-central1-a"
        account = self.runner(
            ["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"],
            check=False,
        ).stdout.strip()
        if not account:
            raise SystemExit("gcloud has no active account. Run `gcloud auth login`.")
        enabled = self._g(
            "services",
            "list",
            "--enabled",
            "--filter=config.name=compute.googleapis.com",
            "--format=value(config.name)",
            check=False,
        )
        if "compute.googleapis.com" not in enabled.stdout:
            if not self.opts.get("yes"):
                raise SystemExit(
                    f"The Compute Engine API is not enabled in {dep.project}. Enable it with\n"
                    f"  gcloud services enable compute.googleapis.com --project {dep.project}\n"
                    "or run again with --yes to let muse-vm enable it."
                )
            self.say("  enabling compute.googleapis.com…")
            self._g("services", "enable", "compute.googleapis.com")
        for kind, args in (
            ("instance", ["compute", "instances", "describe", self.instance, f"--zone={dep.zone}"]),
            ("network", ["compute", "networks", "describe", self.instance]),
        ):
            if self._g(*args, "--format=value(name)", check=False).ok:
                raise SystemExit(
                    f"A {kind} named {self.instance} already exists in {dep.project}. "
                    "Choose another --name or remove it."
                )
        machine = dep.extra["machine_type"]
        cost = MONTHLY_USD.get(machine)
        extras = " + about $3 for the fixed address" if not self.private else " + about $1 for NAT"
        self.say(
            f"  account {account}, project {dep.project}, zone {dep.zone}, {machine}"
            + (f" (about ${cost}/month while running{extras}, $1-2 for the disk)" if cost else "")
        )
        if not self.opts.get("yes"):
            if not sys.stdin.isatty():
                raise SystemExit(
                    "Creating billable cloud resources needs confirmation: run again with --yes."
                )
            if input("  Create these billable resources? [y/N] ").strip().lower() not in (
                "y",
                "yes",
            ):
                raise SystemExit("Aborted.")

    # ---- provisioning ------------------------------------------------------------------------
    def provision(self) -> Target:
        dep, name = self.dep, self.instance
        label = f"mma-deployment={dep.name}"
        region = self.region
        assert dep.zone

        self.say(f"  creating a network of its own, {name}…")
        self._g(
            "compute", "networks", "create", name, "--subnet-mode=custom", f"--description={label}"
        )
        dep.add_resource("network", name)
        self._g(
            "compute",
            "networks",
            "subnets",
            "create",
            name,
            f"--network={name}",
            f"--region={region}",
            f"--range={SUBNET_RANGE}",
            "--enable-private-ip-google-access",
        )
        dep.add_resource("subnet", name, region=region)

        rules = "SSH from Google's IAP range only" + ("" if self.private else ", HTTPS 80/443")
        self.say(f"  creating firewall rules ({rules})…")
        self._g(
            "compute",
            "firewall-rules",
            "create",
            f"{name}-iap-ssh",
            f"--network={name}",
            "--direction=INGRESS",
            "--action=allow",
            "--rules=tcp:22",
            f"--source-ranges={IAP_RANGE}",
            f"--target-tags={name}",
            f"--description={label}",
        )
        dep.add_resource("firewall", f"{name}-iap-ssh")

        if self.private:
            self.say("  no public address: outbound traffic goes through Cloud NAT…")
            self._g("compute", "routers", "create", name, f"--network={name}", f"--region={region}")
            dep.add_resource("router", name, region=region)
            self._g(
                "compute",
                "routers",
                "nats",
                "create",
                name,
                f"--router={name}",
                f"--region={region}",
                "--auto-allocate-nat-external-ips",
                "--nat-all-subnet-ip-ranges",
            )
        else:
            self._g(
                "compute",
                "firewall-rules",
                "create",
                f"{name}-https",
                f"--network={name}",
                "--direction=INGRESS",
                "--action=allow",
                "--rules=tcp:80,tcp:443",
                "--source-ranges=0.0.0.0/0",
                f"--target-tags={name}",
                f"--description={label}",
            )
            dep.add_resource("firewall", f"{name}-https")
            self._g(
                "compute",
                "addresses",
                "create",
                name,
                f"--region={region}",
                f"--description={label}",
            )
            dep.add_resource("address", name, region=region)
            dep.extra["public_ip"] = self._g(
                "compute",
                "addresses",
                "describe",
                name,
                f"--region={region}",
                "--format=value(address)",
            ).stdout.strip()

        machine = dep.extra["machine_type"]
        architecture = "arm64" if machine.startswith(ARM_PREFIXES) else "amd64"
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
            handle.write(bundle.bundle_file("bootstrap.sh"))
            startup = handle.name
        try:
            self.say(
                f"  creating the VM ({machine}, Ubuntu 24.04 {architecture}, no service account)…"
            )
            args = [
                "compute",
                "instances",
                "create",
                name,
                f"--zone={dep.zone}",
                f"--machine-type={machine}",
                f"--image-family=ubuntu-2404-lts-{architecture}",
                "--image-project=ubuntu-os-cloud",
                "--boot-disk-size=20GB",
                "--boot-disk-type=pd-balanced",
                f"--network={name}",
                f"--subnet={name}",
                f"--tags={name}",
                f"--labels={label}",
                "--no-service-account",
                "--no-scopes",
                "--metadata=enable-oslogin=TRUE,block-project-ssh-keys=TRUE,serial-port-enable=FALSE",
                f"--metadata-from-file=startup-script={startup}",
                "--shielded-secure-boot",
                "--shielded-vtpm",
                "--shielded-integrity-monitoring",
                "--no-address" if self.private else f"--address={dep.extra['public_ip']}",
            ]
            self._g(*args)
        finally:
            os.unlink(startup)
        dep.add_resource("instance", name, zone=dep.zone)
        dep.save()
        t = self.target()
        self._wait_for_bootstrap(t)
        t.sh(f"mkdir -p {self.root} && chmod 755 {self.root}", cwd_root=False)
        return t

    def _wait_for_bootstrap(self, t: Target, timeout: float = 900) -> None:
        self.say("  waiting for SSH through IAP and for the startup script (Docker install)…")
        deadline, last = time.monotonic() + timeout, ""
        while time.monotonic() < deadline:
            probe = t.sh(
                "test -f /var/lib/muse-adapter/bootstrap-done && echo done || echo pending",
                cwd_root=False,
                check=False,
                timeout=120,
            )
            if probe.ok and "done" in probe.stdout.split():
                return
            last = ((probe.stderr or probe.stdout).strip().splitlines() or [""])[-1]
            time.sleep(10)
        self.say(f"  the startup script did not finish ({last}); running it over SSH…")
        self.ensure_docker(t)

    # ---- after the adapter runs --------------------------------------------------------------
    def after_start(self, t: Target) -> None:
        if self.opts.get("snapshots", True):
            self._snapshot_schedule()
        hours = self.opts.get("idle_stop_hours")
        if hours:
            self.dep.extra["idle_stop_hours"] = int(hours)
            self.say(
                f"  idle stop: the VM powers off after {hours}h without activity "
                "(`muse-vm start` resumes)"
            )
        budget = self.opts.get("budget_usd")
        if budget:
            self._create_budget(float(budget))

    def _snapshot_schedule(self) -> None:
        """Daily snapshots of the boot disk, which holds the adapter's state, kept for 7 days."""
        name, region = self.instance, self.region
        created = self._g(
            "compute",
            "resource-policies",
            "create",
            "snapshot-schedule",
            name,
            f"--region={region}",
            "--max-retention-days=7",
            "--daily-schedule",
            "--start-time=03:00",
            "--on-source-disk-delete=keep-auto-snapshots",
            check=False,
        )
        if not created.ok:
            self.say(f"  ! the snapshot schedule was not created: {_last_line(created)}")
            return
        self.dep.add_resource("snapshot-schedule", name, region=region)
        attached = self._g(
            "compute",
            "disks",
            "add-resource-policies",
            name,
            f"--zone={self.dep.zone}",
            f"--resource-policies={name}",
            check=False,
        )
        if attached.ok:
            self.dep.extra["snapshots"] = "daily, 7 days"
            self.say("  disk snapshots: daily at 03:00, kept for 7 days")
        else:
            self.say(f"  ! the snapshot schedule could not be attached: {_last_line(attached)}")

    def _create_budget(self, amount: float) -> None:
        enabled = self._g(
            "services",
            "list",
            "--enabled",
            "--filter=config.name=billingbudgets.googleapis.com",
            "--format=value(config.name)",
            check=False,
        )
        if "billingbudgets.googleapis.com" not in enabled.stdout:
            if not self.opts.get("yes"):
                self.say(
                    "  ! the budget alert was skipped: enable the Budget API with "
                    "`gcloud services enable billingbudgets.googleapis.com "
                    f"--project {self.dep.project}` (or run again with --yes)"
                )
                return
            self.say("  enabling billingbudgets.googleapis.com…")
            if not self._g("services", "enable", "billingbudgets.googleapis.com", check=False).ok:
                self.say("  ! the Budget API could not be enabled; no budget alert was created")
                return
        account = (
            self.runner(
                [
                    "gcloud",
                    "billing",
                    "projects",
                    "describe",
                    str(self.dep.project),
                    "--format=value(billingAccountName)",
                ],
                check=False,
            )
            .stdout.strip()
            .split("/")[-1]
        )
        if not account:
            self.say("  ! the billing account could not be read; no budget alert was created")
            return
        argv = [
            "gcloud",
            "billing",
            "budgets",
            "create",
            f"--billing-account={account}",
            f"--display-name=mma-{self.dep.name}",
            f"--budget-amount={amount:g}USD",
            f"--filter-projects=projects/{self.dep.project}",
            "--threshold-rule=percent=0.5",
            "--threshold-rule=percent=0.9",
            "--threshold-rule=percent=1.0",
            "--format=value(name)",
        ]
        result = self.runner(argv, check=False)
        for _ in range(3):  # a just-enabled API can take a minute to be ready
            if result.ok or "SERVICE_DISABLED" not in result.stderr:
                break
            time.sleep(20)
            result = self.runner(argv, check=False)
        if result.ok and result.stdout.strip():
            self.dep.add_resource("budget", result.stdout.strip())
            self.say(
                f"  budget alert: ${amount:g}/month on billing account {account} "
                "(email at 50, 90 and 100%)"
            )
        else:
            self.say(f"  ! the budget alert was not created: {_last_line(result)}")

    # ---- removing it -------------------------------------------------------------------------
    def teardown(self, keep_data: bool = False) -> None:
        dep = self.dep
        ordered = sorted(
            dep.resources,
            key=lambda item: (
                TEARDOWN_ORDER.index(item["kind"]) if item["kind"] in TEARDOWN_ORDER else 99
            ),
        )
        for resource in ordered:
            kind, name = resource["kind"], resource["name"]
            self.say(f"  deleting {kind} {name}…")
            result = self._delete(kind, name, resource, keep_data)
            if result is None:
                continue
            text = (result.stderr + result.stdout).lower()
            if result.ok or "not found" in text or "was not found" in text:
                dep.resources.remove(resource)
                dep.save()
            else:
                raise CommandError(["gcloud", "delete", kind, name], result)

    def _delete(
        self, kind: str, name: str, resource: dict[str, Any], keep_data: bool
    ) -> Result | None:
        zone = resource.get("zone", self.dep.zone)
        region = resource.get("region", self.region)
        if kind == "instance":
            args = ["compute", "instances", "delete", name, f"--zone={zone}"]
            if keep_data:
                args.append("--keep-disks=boot")
                self.say(
                    f"  keeping boot disk {name} (delete it later with "
                    f"`gcloud compute disks delete {name} --zone {zone}`)"
                )
            return self._g(*args, check=False, timeout=900)
        if kind == "snapshot-schedule":
            if keep_data:
                self._g(
                    "compute",
                    "disks",
                    "remove-resource-policies",
                    name,
                    f"--zone={zone}",
                    f"--resource-policies={name}",
                    check=False,
                )
            return self._g(
                "compute", "resource-policies", "delete", name, f"--region={region}", check=False
            )
        if kind == "router":
            return self._g("compute", "routers", "delete", name, f"--region={region}", check=False)
        if kind == "firewall":
            return self._g("compute", "firewall-rules", "delete", name, check=False)
        if kind == "address":
            return self._g(
                "compute", "addresses", "delete", name, f"--region={region}", check=False
            )
        if kind == "subnet":
            return self._g(
                "compute", "networks", "subnets", "delete", name, f"--region={region}", check=False
            )
        if kind == "network":
            return self._g("compute", "networks", "delete", name, check=False)
        if kind == "budget":
            return self.runner(
                ["gcloud", "billing", "budgets", "delete", name, "--quiet"], check=False
            )
        return None

    # ---- running it --------------------------------------------------------------------------
    def vm_status(self) -> str:
        result = self._g(
            "compute",
            "instances",
            "describe",
            self.instance,
            f"--zone={self.dep.zone}",
            "--format=value(status)",
            check=False,
        )
        if not result.ok:
            raise CommandError(
                ["gcloud", "compute", "instances", "describe", self.instance], result
            )
        return result.stdout.strip()

    def ensure_ready(self) -> None:
        state = self.vm_status()
        if state != "RUNNING":
            raise SystemExit(
                f"The VM {self.instance} is {state or 'unknown'}"
                + (" (idle stop, or `muse-vm stop`)" if state == "TERMINATED" else "")
                + f". Start it with: muse-vm start --name {self.dep.name}"
            )

    def start(self) -> None:
        self._g("compute", "instances", "start", self.instance, f"--zone={self.dep.zone}")

    def stop(self) -> None:
        self._g("compute", "instances", "stop", self.instance, f"--zone={self.dep.zone}")

    def status(self) -> dict[str, Any]:
        result = self._g(
            "compute",
            "instances",
            "describe",
            self.instance,
            f"--zone={self.dep.zone}",
            "--format=value(status)",
            check=False,
        )
        idle = self.dep.extra.get("idle_stop_hours")
        return {
            "project": self.dep.project,
            "zone": self.dep.zone,
            "instance": self.instance,
            "machine type": self.dep.extra.get("machine_type"),
            "VM status": result.stdout.strip() or "unknown",
            "public IP": self.dep.extra.get("public_ip", "none (Cloud NAT)"),
            "disk snapshots": self.dep.extra.get("snapshots", "off"),
            "idle stop": f"{idle}h" if idle else "off",
            "resources": ", ".join(f"{r['kind']}:{r['name']}" for r in self.dep.resources),
        }


def _last_line(result: Result) -> str:
    return ((result.stderr or result.stdout).strip().splitlines() or [""])[-1]
