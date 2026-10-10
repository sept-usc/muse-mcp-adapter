"""The foundations of muse-vm: running commands, remembered state, targets, encryption, ingress."""

import json
import stat
import sys
from pathlib import Path

import pytest

from muse_vm import crypt, ingress
from muse_vm.runner import CommandError, Result, Runner
from muse_vm.state import Deployment, check_name
from muse_vm.targets import GcloudTarget, SSHTarget
from tests.fixtures.vm_fakes import FakeRunner, FakeTarget


# ---- running commands -------------------------------------------------------------------------
def test_a_failing_command_reports_what_ran_and_why() -> None:
    with pytest.raises(CommandError) as raised:
        Runner()([sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"])
    assert raised.value.result.returncode == 3 and "boom" in str(raised.value)


def test_input_reaches_the_command_on_stdin_not_in_its_arguments() -> None:
    result = Runner()(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], input="secret"
    )
    assert result.stdout.strip() == "SECRET"


def test_a_missing_program_and_a_timeout_are_reported_not_raised_as_crashes() -> None:
    assert Runner()(["no-such-program-xyz"], check=False).returncode == 127
    slow = Runner()([sys.executable, "-c", "import time; time.sleep(5)"], check=False, timeout=0.2)
    assert slow.returncode == 124


# ---- remembered state -------------------------------------------------------------------------
def test_state_is_saved_privately_and_reloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MUSE_VM_HOME", str(tmp_path))
    dep = Deployment(name="muse", provider="gcp", ingress="caddy", hostname="a.example.com")
    dep.add_resource("network", "mma-muse")
    dep.add_resource("instance", "mma-muse", zone="us-central1-a")

    path = Deployment.path_for("muse")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again = Deployment.load("muse")
    assert [r["kind"] for r in again.resources] == ["network", "instance"]
    assert again.base_url == "https://a.example.com"
    assert [d.name for d in Deployment.all()] == ["muse"]
    dep.delete()
    assert Deployment.all() == []


def test_state_holds_no_secret_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSE_VM_HOME", str(tmp_path))
    Deployment(name="muse", provider="ssh", ingress="caddy").save()
    saved = json.loads(Deployment.path_for("muse").read_text())
    assert not any("token" in key or "secret" in key or "password" in key for key in saved)


def test_an_unknown_deployment_says_how_to_find_the_known_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MUSE_VM_HOME", str(tmp_path))
    with pytest.raises(SystemExit, match="muse-vm list"):
        Deployment.load("ghost")


@pytest.mark.parametrize("name", ["", "Muse", "1muse", "a" * 21, "has space", "../x"])
def test_deployment_names_are_restricted(name: str) -> None:
    with pytest.raises(SystemExit):
        check_name(name)


# ---- targets ----------------------------------------------------------------------------------
def test_an_ssh_command_runs_through_sudo_with_batch_mode() -> None:
    runner = FakeRunner()
    SSHTarget(runner, "me@host", ["-p", "2222"]).sh("echo hi")  # type: ignore[arg-type]
    argv = runner.calls[0]["argv"]
    assert argv[0] == "ssh" and "BatchMode=yes" in argv and "2222" in argv
    assert argv[-1].startswith("sudo -n sh -c ") and "echo hi" in argv[-1]
    assert "cd /opt/muse-adapter" in argv[-1]


def test_a_gcp_command_goes_through_iap_and_never_needs_a_public_ssh_port() -> None:
    runner = FakeRunner()
    GcloudTarget(runner, "mma-muse", "us-central1-a", "proj").sh("true")  # type: ignore[arg-type]
    argv = runner.calls[0]["argv"]
    assert argv[:4] == ["gcloud", "compute", "ssh", "mma-muse"]
    assert "--tunnel-through-iap" in argv and "--project=proj" in argv


def test_writing_a_file_sends_its_content_on_stdin_and_moves_it_into_place() -> None:
    runner = FakeRunner()
    target = SSHTarget(runner, "me@host")  # type: ignore[arg-type]
    target.put("secrets/KEY", "value\n", 0o400, owner="10001:10001")
    call = runner.calls[0]
    assert call["input"] == "value\n"
    script = call["argv"][-1]
    assert "chmod 400" in script and "chown 10001:10001" in script and "mv -f" in script
    assert "value" not in " ".join(call["argv"])


def test_binary_content_survives_the_text_channel() -> None:
    import base64

    runner = FakeRunner()
    payload = bytes(range(256)) * 4
    runner.when("base64 | tr", stdout=base64.b64encode(payload).decode())
    assert SSHTarget(runner, "me@host").get_bytes("tar -czf - x") == payload  # type: ignore[arg-type]


# ---- backup encryption ------------------------------------------------------------------------
def test_an_encrypted_backup_round_trips_and_hides_its_content() -> None:
    blob = crypt.encrypt(b"adapter.key and tokens", "correct horse battery")
    assert b"adapter.key" not in blob and blob.startswith(crypt.MAGIC)
    assert crypt.decrypt(blob, "correct horse battery") == b"adapter.key and tokens"


def test_the_wrong_passphrase_and_a_tampered_file_are_refused() -> None:
    blob = crypt.encrypt(b"data", "correct horse battery")
    with pytest.raises(SystemExit, match="Wrong passphrase"):
        crypt.decrypt(blob, "wrong passphrase here")
    flipped = bytearray(blob)
    flipped[-1] ^= 1
    with pytest.raises(SystemExit, match="changed"):
        crypt.decrypt(bytes(flipped), "correct horse battery")
    with pytest.raises(SystemExit, match="not a muse-vm backup"):
        crypt.decrypt(b"plain text", "correct horse battery")


def test_the_same_data_encrypts_differently_each_time() -> None:
    assert crypt.encrypt(b"x", "a long enough passphrase") != crypt.encrypt(
        b"x", "a long enough passphrase"
    )


def test_a_weak_passphrase_is_refused() -> None:
    with pytest.raises(SystemExit, match="12 characters"):
        crypt.encrypt(b"x", "short")


# ---- ingress ----------------------------------------------------------------------------------
def test_a_public_address_becomes_a_hostname_that_can_get_a_certificate() -> None:
    assert ingress.sslip_name("203.0.113.7") == "203-0-113-7.sslip.io"
    assert (
        ingress.hostname_before_start("caddy", domain=None, public_ip="34.1.2.3")
        == "34-1-2-3.sslip.io"
    )
    assert (
        ingress.hostname_before_start("caddy", domain="adapter.me.dev", public_ip=None)
        == "adapter.me.dev"
    )


@pytest.mark.parametrize("address", [None, "10.0.0.5", "192.168.1.9", "100.64.0.1", "127.0.0.1"])
def test_caddy_without_a_public_address_is_refused_with_advice(address: str | None) -> None:
    with pytest.raises(SystemExit, match="public IP"):
        ingress.hostname_before_start("caddy", domain=None, public_ip=address)


def test_a_cloudflare_tunnel_needs_its_hostname_up_front() -> None:
    with pytest.raises(SystemExit, match="--domain"):
        ingress.hostname_before_start("cloudflare-tunnel", domain=None, public_ip=None)


def test_the_tailscale_hostname_is_learned_once_it_is_running() -> None:
    target = FakeTarget()
    target.answer("tailscale status", stdout=json.dumps({"BackendState": "NeedsLogin"}))
    target.answer("logs", stdout="login: https://login.tailscale.com/a/abc123XYZ")
    seen: list[str] = []
    states = iter(
        [
            json.dumps({"BackendState": "NeedsLogin"}),
            json.dumps(
                {"BackendState": "Running", "Self": {"DNSName": "muse-adapter-muse.tail1.ts.net."}}
            ),
        ]
    )

    original = target.sh

    def sh(script: str, *a: object, **k: object):  # type: ignore[no-untyped-def]
        if "tailscale status" in script:
            from muse_vm.runner import Result

            return Result(0, next(states), "")
        return original(script, *a, **k)  # type: ignore[arg-type]

    target.sh = sh  # type: ignore[method-assign]
    name = ingress.hostname_after_start(
        target, "tailscale-funnel", say=seen.append, sleep=lambda _: None
    )
    assert name == "muse-adapter-muse.tail1.ts.net"
    assert any(
        "login.tailscale.com/a/abc123XYZ" in line for line in seen
    )  # the login link was shown


def test_a_tailscale_that_never_signs_in_ends_with_a_clear_message() -> None:
    target = FakeTarget()
    target.answer("tailscale status", stdout=json.dumps({"BackendState": "NeedsLogin"}))
    with pytest.raises(SystemExit, match="did not appear"):
        ingress.hostname_after_start(target, "tailscale-funnel", timeout=0.05, sleep=lambda _: None)


# ---- dropped connections are retried ---------------------------------------------------------
def test_a_dropped_connection_is_retried_but_a_failing_command_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from muse_vm import targets

    pauses: list[float] = []
    monkeypatch.setattr(targets, "_pause", pauses.append)

    class Flaky(FakeRunner):
        def __init__(self, codes: list[int]) -> None:
            super().__init__()
            self.codes = codes

        def __call__(self, argv, input=None, check=True, timeout=None, stream=False):  # type: ignore[no-untyped-def]
            self.calls.append({"argv": list(argv), "input": input, "check": check})
            return Result(self.codes.pop(0) if self.codes else 0, "out", "err")

    flaky = Flaky([255, 255, 0])
    assert SSHTarget(flaky, "me@host").sh("true").stdout == "out"  # type: ignore[arg-type]
    assert len(flaky.calls) == 3 and len(pauses) == 2

    broken = Flaky([255] * 10)
    with pytest.raises(CommandError, match="255"):
        SSHTarget(broken, "me@host").sh("true")  # type: ignore[arg-type]
    assert len(broken.calls) == len(targets.RETRY_DELAYS) + 1  # gives up, and says so

    failing = Flaky([1])
    with pytest.raises(CommandError, match="command failed"):
        SSHTarget(failing, "me@host").sh("false")  # type: ignore[arg-type]
    assert len(failing.calls) == 1  # a command that ran and failed is not repeated
