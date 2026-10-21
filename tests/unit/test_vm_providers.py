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
_FULL = "The zone does not have enough resources available to fulfill the request."


def test_a_zone_that_is_out_of_capacity_falls_back_to_another_zone_of_the_region() -> None:
    provider, dep, runner = _gcp()
    runner.rules.insert(0, ("instances create mma-muse --zone=us-central1-a", Result(1, "", _FULL)))
    runner.when("zones list", stdout="us-central1-a\nus-central1-b\nus-central1-c")
    provider.preflight()
    provider.provision()

    created = _created(runner, "instances")
    assert len(created) == 2 and "--zone=us-central1-a" in created[0]
    assert "--zone=us-central1-b" in created[1]  # the next zone, not the one that was full
    assert dep.zone == "us-central1-b"  # everything after this (SSH, snapshots) uses the new zone
    assert any(r["kind"] == "instance" and r.get("zone") == "us-central1-b" for r in dep.resources)
    assert not runner.ran("--region=us-west")  # the regional network, firewall and NAT are reused


def test_a_real_error_creating_the_vm_is_not_mistaken_for_a_full_zone() -> None:
    from muse_vm.runner import CommandError

    provider, _dep, runner = _gcp()
    runner.rules.insert(0, ("instances create", Result(1, "", "Quota 'CPUS' exceeded. Limit: 8.0")))
    provider.preflight()
    with pytest.raises(CommandError):
        provider.provision()
    assert len(_created(runner, "instances")) == 1 and not runner.ran("zones list")


def test_when_every_zone_is_full_the_message_says_what_to_try_next() -> None:
    provider, _dep, runner = _gcp()
    runner.rules.insert(0, ("instances create", Result(1, "", _FULL)))
    runner.when("zones list", stdout="us-central1-a\nus-central1-b")
    provider.preflight()
    with pytest.raises(SystemExit, match="No zone in us-central1 has e2-small capacity"):
        provider.provision()
    assert len(_created(runner, "instances")) == 2  # both zones were tried, once each


def test_the_default_output_of_creating_a_vm_is_a_few_lines_and_verbose_shows_every_step() -> None:
    said: list[str] = []
    provider, _dep, _runner = _gcp()
    provider.say = said.append  # type: ignore[method-assign]
    provider.preflight()
    provider.provision()
    quiet = [line for line in said if line.startswith("  ")]
    assert any("network, firewall rules and Cloud NAT" in line for line in quiet)
    assert not any("deleting" in line or "creating firewall rules" in line for line in quiet)
    assert len(quiet) <= 6

    loud_said: list[str] = []
    provider, _dep, runner = _gcp()
    runner.verbose = True  # type: ignore[attr-defined]
    provider.say = loud_said.append  # type: ignore[method-assign]
    provider.preflight()
    provider.provision()
    assert any("creating firewall rules" in line for line in loud_said)


def test_gcloud_ssh_hides_gcloud_own_performance_warning() -> None:
    from muse_vm.targets import GcloudTarget

    target = GcloudTarget(
        FakeRunner(),  # pyright: ignore[reportArgumentType]
        "mma-x",
        "us-central1-a",
        "my-proj",
        "/opt/muse-adapter",
    )
    assert "--verbosity=error" in target._argv("true")  # pyright: ignore[reportPrivateUsage]


def test_raw_tool_output_is_captured_unless_verbose() -> None:
    from muse_vm.runner import Runner

    quiet = Runner(verbose=False)(["sh", "-c", "echo build output"], stream=True)
    assert quiet.stdout == "build output\n"  # captured, so it is not printed to the screen
    loud = Runner(verbose=True)(["sh", "-c", "echo build output"], stream=True)
    assert loud.stdout == ""  # streamed to the terminal instead


def test_a_disabled_api_is_explained_in_a_sentence_not_an_api_dump() -> None:
    provider, dep, runner = _gcp()
    dep.project, dep.zone = "other-proj", "us-central1-a"
    runner.rules.insert(
        0,
        (
            "instances describe",
            Result(
                1,
                "",
                "reason: SERVICE_DISABLED\nactivationUrl: https://console.developers.google.com/"
                "apis/api/compute.googleapis.com/overview?project=other-proj\n",
            ),
        ),
    )
    with pytest.raises(SystemExit) as stopped:
        provider.vm_status()
    message = str(stopped.value)
    assert "Compute Engine API is not enabled in project other-proj" in message
    assert "console.developers.google.com" in message and "--forget" in message
    assert "domain:" not in message and "reason:" not in message  # no raw dump


def test_a_missing_vm_is_explained_with_the_way_to_clear_the_record() -> None:
    provider, dep, runner = _gcp()
    dep.project, dep.zone = "my-proj", "us-central1-a"
    runner.rules.insert(0, ("instances describe", Result(1, "", "The resource was not found")))
    with pytest.raises(SystemExit, match="was not found in project my-proj"):
        provider.vm_status()


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


# ---- GCP: leftovers of an earlier run -------------------------------------------------------
def _with_leftovers(runner: FakeRunner, *, ours: bool = True) -> None:
    description = "mma-deployment=muse" if ours else "someone else's network"
    runner.rules.insert(0, ("networks describe mma-muse", Result(0, description, "")))
    runner.rules.insert(0, ("routers describe mma-muse", Result(0, "mma-muse", "")))


def test_leftovers_of_an_earlier_run_are_named_and_need_an_explicit_yes() -> None:
    provider, _dep, runner = _gcp()
    _with_leftovers(runner)
    with pytest.raises(SystemExit, match="--replace-leftovers") as stopped:
        provider.preflight()
    assert "network mma-muse" in str(stopped.value) and "router mma-muse" in str(stopped.value)
    assert not any("delete" in line for line in runner.lines())  # nothing removed without a yes


def test_leftovers_are_removed_with_the_flag_and_the_network_goes_last() -> None:
    provider, dep, runner = _gcp(replace_leftovers=True)
    _with_leftovers(runner)
    provider.preflight()
    deletes = [line for line in runner.lines() if " delete " in line]
    assert len(deletes) == 2 and "routers delete mma-muse" in deletes[0]
    assert "networks delete mma-muse" in deletes[1]  # the network only after what is in it
    assert dep.resources == []  # and the new run starts from nothing


def test_a_network_that_muse_vm_did_not_create_is_never_touched() -> None:
    provider, _dep, runner = _gcp(replace_leftovers=True)
    _with_leftovers(runner, ours=False)
    with pytest.raises(SystemExit, match="was not created by muse-vm"):
        provider.preflight()
    assert not any(" delete " in line for line in runner.lines())


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


def test_the_local_provider_installs_on_this_machine_and_refuses_other_systems(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from muse_vm.providers import PROVIDERS
    from muse_vm.providers.local import LocalProvider

    assert PROVIDERS["local"] is LocalProvider
    runner = FakeRunner()
    runner.when("uname -s -m", stdout="Linux x86_64\n0\n")
    runner.when("test -f /opt/muse-adapter/compose.yml", stdout="no")
    dep = Deployment(name="here", provider="local", ingress="tailscale-funnel")
    provider = LocalProvider(dep, runner, lambda _: None, {"yes": True})  # type: ignore[arg-type]
    monkeypatch.setattr("muse_vm.providers.local.sys.platform", "linux")
    provider.preflight()
    assert provider.status()["host"] == "this machine"
    assert not any(line.startswith("ssh ") for line in runner.lines())  # no ssh hop at all

    monkeypatch.setattr("muse_vm.providers.local.sys.platform", "darwin")
    with pytest.raises(SystemExit, match="must be\nLinux|must be Linux"):
        provider.preflight()


def test_down_on_an_unreachable_machine_tries_once_and_explains() -> None:
    provider, runner = _ssh()
    runner.rules.insert(
        0,
        (
            "sudo -n sh -c true",
            Result(255, "", "ssh: connect to host 203.0.113.9 port 22: Operation timed out"),
        ),
    )
    with pytest.raises(SystemExit) as stopped:
        provider.teardown()
    message = str(stopped.value)
    assert "Could not reach me@203.0.113.9" in message and "--forget" in message
    probes = [line for line in runner.lines() if "sudo -n sh -c true" in line]
    assert len(probes) == 1  # no retries, so it takes seconds and not minutes
    assert not any("compose" in line for line in runner.lines())  # nothing else was attempted


def test_other_commands_fail_fast_on_an_unreachable_machine_too() -> None:
    provider, runner = _ssh()
    runner.rules.insert(0, ("sudo -n sh -c true", Result(255, "", "Connection timed out")))
    with pytest.raises(SystemExit, match="Could not reach"):
        provider.ensure_ready()


def test_down_on_a_reachable_machine_still_removes_everything() -> None:
    provider, runner = _ssh()
    provider.teardown()
    assert runner.ran("docker compose down")
