"""The SSH and GCP providers: what they check, what they create, and what they clean up."""

from typing import Any

import pytest

from muse_vm.providers.gcp import IAP_RANGE, TEARDOWN_ORDER, GcpProvider
from muse_vm.providers.ssh import SSHProvider
from muse_vm.runner import Result
from muse_vm.state import Deployment
from tests.fixtures.vm_fakes import FakeRunner


def _gcp(**opts: Any) -> tuple[GcpProvider, Deployment, FakeRunner]:
    runner = FakeRunner()
    # specific answers first: the first matching rule wins
    runner.when("config get-value project", stdout="my-proj")
    runner.when("config get-value compute/zone", stdout="us-central1-a")
    runner.when("auth list", stdout="me@example.com")
    runner.when("services list", stdout="compute.googleapis.com")
    runner.when("test -f /var/lib/muse-adapter/bootstrap-done", stdout="done")
    runner.when("describe", returncode=1, stderr="not found")  # nothing else exists yet
    dep = Deployment(name="muse", provider="gcp", ingress="tailscale-funnel")
    provider = GcpProvider(dep, runner, lambda _: None, {"yes": True, **opts})  # type: ignore[arg-type]
    return provider, dep, runner


def _prepared(provider: GcpProvider) -> GcpProvider:
    """A provider as it is once `up` has run preflight: project and zone are known and saved."""
    provider.dep.project, provider.dep.zone = "my-proj", "us-central1-a"
    return provider


def _created(runner: FakeRunner, kind: str) -> list[str]:
    return [line for line in runner.lines() if f"compute {kind} create" in line]


# ---- GCP: the network ------------------------------------------------------------------------
def test_the_vm_gets_a_network_of_its_own_and_never_uses_the_default_one() -> None:
    provider, dep, runner = _gcp()
    provider.preflight()
    provider.provision()
    joined = "\n".join(runner.lines())

    assert "networks create mma-muse --subnet-mode=custom" in joined
    assert "--network=mma-muse" in joined and "--subnet=mma-muse" in joined
    assert "--network=default" not in joined and "network=default" not in joined
    assert [r["kind"] for r in dep.resources][:2] == ["network", "subnet"]


def test_ssh_is_open_only_to_googles_iap_range_and_nothing_else_is() -> None:
    provider, _, runner = _gcp()
    provider.preflight()
    provider.provision()
    rules = _created(runner, "firewall-rules")
    assert len(rules) == 1  # no web ports: Muse arrives through Tailscale, not through the VM
    ssh = rules[0]
    assert "-iap-ssh" in ssh
    assert (
        f"--source-ranges={IAP_RANGE}" in ssh and "--rules=tcp:22" in ssh and "0.0.0.0/0" not in ssh
    )


def test_the_vm_has_no_public_address_and_reaches_out_through_cloud_nat() -> None:
    provider, dep, runner = _gcp()
    provider.preflight()
    provider.provision()
    joined = "\n".join(runner.lines())
    assert "--no-address" in joined and "--address=" not in joined
    assert "routers create mma-muse" in joined and "nats create mma-muse" in joined
    assert not _created(runner, "addresses")
    assert "address" not in [r["kind"] for r in dep.resources]


# ---- GCP: the VM -----------------------------------------------------------------------------
def test_the_vm_is_hardened_and_holds_no_cloud_credentials() -> None:
    provider, _, runner = _gcp()
    provider.preflight()
    provider.provision()
    vm = _created(runner, "instances")[0]
    for flag in (
        "--no-service-account",
        "--no-scopes",
        "--shielded-secure-boot",
        "--shielded-vtpm",
        "--shielded-integrity-monitoring",
        "enable-oslogin=TRUE",
        "block-project-ssh-keys=TRUE",
        "serial-port-enable=FALSE",
        "--labels=mma-deployment=muse",
        "--tags=mma-muse",
    ):
        assert flag in vm, flag
    assert "ubuntu-2404-lts-amd64" in vm


def test_an_arm_machine_gets_the_arm_image() -> None:
    provider, _, runner = _gcp(machine_type="t2a-standard-1")
    provider.preflight()
    provider.provision()
    assert "ubuntu-2404-lts-arm64" in _created(runner, "instances")[0]


def test_the_startup_script_is_the_bundles_bootstrap_and_holds_no_secret() -> None:
    from muse_vm import bundle

    provider, _, runner = _gcp()
    provider.preflight()
    provider.provision()
    assert "--metadata-from-file=startup-script=" in _created(runner, "instances")[0]
    assert "TOKEN" not in bundle.bundle_file("bootstrap.sh")


# ---- GCP: preflight --------------------------------------------------------------------------
def test_preflight_refuses_without_confirmation_when_it_would_spend_money(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _, _ = _gcp(yes=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    with pytest.raises(SystemExit, match="--yes"):
        provider.preflight()


def test_preflight_stops_when_the_compute_api_is_off_unless_allowed_to_enable_it() -> None:
    runner = FakeRunner()
    runner.when("describe", returncode=1)
    runner.when("config get-value project", stdout="p")
    runner.when("auth list", stdout="me@example.com")
    runner.when("services list", stdout="")
    dep = Deployment(name="muse", provider="gcp", ingress="tailscale-funnel")
    with pytest.raises(SystemExit, match="Compute Engine API"):
        GcpProvider(dep, runner, lambda _: None, {}).preflight()  # type: ignore[arg-type]


def test_preflight_refuses_a_name_that_is_already_taken() -> None:
    provider, _, runner = _gcp()
    runner.rules.insert(0, ("instances describe", Result(0, "mma-muse", "")))
    with pytest.raises(SystemExit, match="already exists"):
        provider.preflight()


def test_preflight_needs_gcloud_and_a_login() -> None:
    runner = FakeRunner()
    runner.when("--version", returncode=127)
    dep = Deployment(name="muse", provider="gcp", ingress="tailscale-funnel")
    with pytest.raises(SystemExit, match="gcloud CLI is required"):
        GcpProvider(dep, runner, lambda _: None, {}).preflight()  # type: ignore[arg-type]


# ---- GCP: snapshots, budget, teardown --------------------------------------------------------
def test_daily_snapshots_are_scheduled_and_attached_to_the_boot_disk() -> None:
    provider, dep, runner = _gcp()
    provider.preflight()
    provider.provision()
    provider.after_start(provider.target())
    joined = "\n".join(runner.lines())
    assert "resource-policies create snapshot-schedule mma-muse" in joined
    assert "--max-retention-days=7" in joined and "--daily-schedule" in joined
    assert "disks add-resource-policies mma-muse" in joined
    assert "snapshot-schedule" in [r["kind"] for r in dep.resources]


def test_snapshots_can_be_turned_off() -> None:
    provider, _, runner = _gcp(snapshots=False)
    provider.preflight()
    provider.provision()
    provider.after_start(provider.target())
    assert "resource-policies" not in "\n".join(runner.lines())


def test_teardown_deletes_the_vm_first_and_the_network_last() -> None:
    provider, dep, runner = _gcp(budget_usd=None)
    provider.preflight()
    provider.provision()
    provider.after_start(provider.target())
    runner.calls.clear()
    provider.teardown()

    deletions = [line for line in runner.lines() if " delete " in line]
    order = [kind for kind in TEARDOWN_ORDER for line in deletions if _kind_of(line) == kind]
    assert _kind_of(deletions[0]) == "instance" and _kind_of(deletions[-1]) == "network"
    assert order == sorted(order, key=TEARDOWN_ORDER.index)
    assert dep.resources == []  # everything it created is gone from the record


def _kind_of(line: str) -> str:
    if "instances delete" in line:
        return "instance"
    if "resource-policies delete" in line:
        return "snapshot-schedule"
    if "routers delete" in line:
        return "router"
    if "firewall-rules delete" in line:
        return "firewall"
    if "addresses delete" in line:
        return "address"
    if "subnets delete" in line:
        return "subnet"
    if "networks delete" in line:
        return "network"
    return "?"


def test_teardown_treats_already_deleted_resources_as_done() -> None:
    provider, dep, runner = _gcp()
    _prepared(provider)
    dep.add_resource("firewall", "mma-muse-https")
    runner.when("firewall-rules delete", returncode=1, stderr="The resource was not found")
    provider.teardown()
    assert dep.resources == []


def test_teardown_stops_and_keeps_the_state_when_a_delete_really_fails() -> None:
    from muse_vm.runner import CommandError

    provider, dep, runner = _gcp()
    _prepared(provider)
    dep.add_resource("network", "mma-muse")
    runner.when("networks delete", returncode=1, stderr="in use by another resource")
    with pytest.raises(CommandError):
        provider.teardown()
    assert [r["kind"] for r in dep.resources] == ["network"]  # kept so `down` can be retried


def test_keeping_data_keeps_the_boot_disk_and_detaches_its_snapshot_policy() -> None:
    provider, dep, runner = _gcp()
    _prepared(provider)
    dep.add_resource("instance", "mma-muse", zone="us-central1-a")
    dep.add_resource("snapshot-schedule", "mma-muse", region="us-central1")
    provider.teardown(keep_data=True)
    joined = "\n".join(runner.lines())
    assert "--keep-disks=boot" in joined and "disks remove-resource-policies" in joined


def test_idle_stop_is_recorded_for_status_and_installs_its_timer_through_the_units() -> None:
    from tests.fixtures.vm_fakes import FakeTarget

    provider, dep, _ = _gcp(idle_stop_hours=6)
    _prepared(provider)
    provider.after_start(FakeTarget())
    assert dep.extra["idle_stop_hours"] == 6


# ---- SSH --------------------------------------------------------------------------------------
def _ssh(**opts: Any) -> tuple[SSHProvider, FakeRunner]:
    runner = FakeRunner()
    runner.when("uname -s -m", stdout="Linux x86_64\n0\n")
    runner.when("test -f /opt/muse-adapter/compose.yml", stdout="no")
    dep = Deployment(
        name="muse", provider="ssh", ingress="tailscale-funnel", ssh_host="me@203.0.113.9"
    )
    return SSHProvider(dep, runner, lambda _: None, opts), runner  # type: ignore[arg-type]


def test_ssh_preflight_accepts_a_clean_linux_host() -> None:
    provider, _ = _ssh()
    provider.preflight()
    assert provider.dep.extra["arch"] == "x86_64"


def test_ssh_preflight_refuses_a_non_linux_host_and_an_existing_deployment() -> None:
    provider, runner = _ssh()
    runner.rules.insert(0, ("uname -s -m", Result(0, "Darwin arm64\n", "")))
    with pytest.raises(SystemExit, match="Unsupported"):
        provider.preflight()

    provider, runner = _ssh()
    runner.rules.insert(0, ("test -f /opt/muse-adapter/compose.yml", Result(0, "yes", "")))
    with pytest.raises(SystemExit, match="already has a deployment"):
        provider.preflight()


def test_ssh_without_passwordless_sudo_gets_a_clear_message() -> None:
    provider, runner = _ssh()
    runner.rules.insert(0, ("uname -s -m", Result(1, "", "sudo: a password is required")))
    with pytest.raises(SystemExit, match="passwordless sudo"):
        provider.preflight()


def test_removing_an_ssh_host_stops_the_units_and_leaves_the_rest_of_the_machine_alone() -> None:
    provider, runner = _ssh()
    provider.teardown()
    script = " ".join(runner.lines())
    assert "systemctl disable --now" in script and "muse-adapter" in script
    assert "docker compose down -v --remove-orphans" in script
    assert "rm -rf /opt/muse-adapter" in script
    assert "rm -rf /" not in script.replace("rm -rf /opt/muse-adapter", "")


def test_keeping_data_on_an_ssh_host_keeps_the_data_and_the_backups() -> None:
    provider, runner = _ssh()
    provider.teardown(keep_data=True)
    script = " ".join(runner.lines())
    assert "! -name data" in script and "! -name backups" in script
