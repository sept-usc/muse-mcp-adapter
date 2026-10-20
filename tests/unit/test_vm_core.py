"""The foundations of muse-vm: running commands, remembered state, targets, encryption, ingress."""

import json
import stat
import sys
from pathlib import Path

import pytest

from muse_vm import crypt
from muse_vm.runner import CommandError, Result, Runner
from muse_vm.state import Deployment, check_name
from muse_vm.targets import GcloudTarget, SSHTarget
from tests.fixtures.vm_fakes import FakeRunner


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


def _no_pause(_seconds: float) -> None:
    return None


# ---- the machine you are on ------------------------------------------------------------------
def test_local_commands_run_directly_as_root_or_through_sudo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from muse_vm.targets import LocalTarget

    runner = FakeRunner()
    monkeypatch.setattr("muse_vm.targets.os.geteuid", lambda: 0)
    LocalTarget(runner).sh("true")  # type: ignore[arg-type]
    assert runner.calls[-1]["argv"][:2] == ["sh", "-c"]
    monkeypatch.setattr("muse_vm.targets.os.geteuid", lambda: 1000)
    LocalTarget(runner).sh("true")  # type: ignore[arg-type]
    assert runner.calls[-1]["argv"][:4] == ["sudo", "-n", "sh", "-c"]
    assert "this machine" in LocalTarget(runner).describe()  # type: ignore[arg-type]


def test_a_failing_local_command_is_not_retried_like_a_dropped_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from muse_vm import targets
    from muse_vm.targets import LocalTarget

    monkeypatch.setattr(targets, "_pause", _no_pause)
    runner = FakeRunner([("sh -c", Result(255, "", "exit 255 is this command's own answer"))])
    with pytest.raises(CommandError):
        LocalTarget(runner).sh("exit 255")  # type: ignore[arg-type]
    assert len(runner.calls) == 1
