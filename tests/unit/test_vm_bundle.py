"""The deployment bundle: its hardening, its files, and its scripts."""

import configparser
import io
import json
import os
import stat
import subprocess
import tarfile
import time
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import yaml

from muse_mcp_adapter.application.serve import load_serve_config
from muse_vm import bundle
from muse_vm.targets import SSHTarget
from tests.fixtures.vm_fakes import FakeRunner, FakeTarget

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "src" / "muse_vm" / "bundle"


def _compose() -> dict[str, Any]:
    return yaml.safe_load((BUNDLE / "compose.yml").read_text())


# ---- the compose file -----------------------------------------------------------------------
def test_the_adapter_and_guard_are_hardened() -> None:
    services = _compose()["services"]
    for name in ("adapter", "guard"):
        service = services[name]
        assert service["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in service["security_opt"], name
        assert service["read_only"] is True, name
        assert (
            service["logging"]["options"]["max-size"] and service["logging"]["options"]["max-file"]
        )
        assert service["mem_limit"] and service["pids_limit"], name
    assert services["adapter"]["user"] == "10001:10001"
    assert services["guard"]["cap_add"] == ["NET_ADMIN"]  # the only added capability: the guard's
    assert "cap_add" not in services["adapter"]


def test_nothing_is_privileged_or_exposes_the_host() -> None:
    text = (BUNDLE / "compose.yml").read_text()
    assert (
        "privileged" not in text and "docker.sock" not in text and "network_mode: host" not in text
    )
    for service in _compose()["services"].values():
        assert not any(
            str(v).startswith(("/:", "/etc", "/var/run")) for v in service.get("volumes", [])
        )


def test_nothing_publishes_a_port_on_the_host() -> None:
    # Muse reaches the adapter through Tailscale Funnel, which needs no inbound connection.
    services = _compose()["services"]
    assert [name for name, service in services.items() if "ports" in service] == []
    assert set(services) == {"guard", "adapter", "tailscale"}
    assert "profiles" not in services["tailscale"]


def test_the_adapter_waits_for_the_guard_and_the_ingress_for_the_adapter() -> None:
    services = _compose()["services"]
    assert services["adapter"]["network_mode"] == "service:guard"
    assert services["adapter"]["depends_on"]["guard"]["condition"] == "service_healthy"
    assert services["tailscale"]["depends_on"]["adapter"]["condition"] == "service_healthy"


def test_every_third_party_image_is_pinned_by_digest() -> None:
    text = (BUNDLE / "compose.yml").read_text()
    images = [line.split("image:", 1)[1].strip() for line in text.splitlines() if "image:" in line]
    third_party = [i for i in images if "MMA_IMAGE" not in i and "MMA_GUARD_IMAGE" not in i]
    assert len(third_party) == 1  # Tailscale
    for image in third_party:
        assert "@sha256:" in image and ":latest" not in image, image


def test_the_adapter_receives_state_config_and_secrets_the_right_way() -> None:
    volumes = _compose()["services"]["adapter"]["volumes"]
    assert "./config:/config:ro" in volumes and "./secrets:/run/secrets:ro" in volumes
    assert "./data:/data" in volumes
    assert (
        _compose()["services"]["adapter"]["environment"]["MUSE_ADAPTER_SECRETS_DIR"]
        == "/run/secrets"
    )


def test_the_build_override_builds_both_images_from_the_uploaded_source() -> None:
    services = yaml.safe_load((BUNDLE / "compose.build.yml").read_text())["services"]
    assert services["adapter"]["build"]["context"] == "./src"
    assert services["guard"]["build"]["context"] == "./src/guard"


# ---- the environment and configuration the CLI renders --------------------------------------
def test_the_environment_names_the_images_and_leaves_no_profile_to_choose() -> None:
    env = bundle.compose_env(project="muse-adapter-a", build=True)
    assert env["COMPOSE_FILE"] == "compose.yml:compose.build.yml"
    assert "COMPOSE_PROFILES" not in env and "MMA_HOSTNAME" not in env
    assert env["MMA_IMAGE"].startswith("muse-adapter-local/")
    assert env["MMA_IMAGE"].endswith(f":{bundle.__version__}") and ":latest" not in env["MMA_IMAGE"]


def test_images_from_your_own_registry_need_both_names() -> None:
    env = bundle.compose_env(
        project="p",
        build=False,
        image="registry.example.com/adapter@sha256:" + "a" * 64,
        guard_image="registry.example.com/guard@sha256:" + "b" * 64,
    )
    assert env["COMPOSE_FILE"] == "compose.yml" and "registry.example.com" in env["MMA_IMAGE"]
    with pytest.raises(SystemExit, match="--image and --guard-image"):
        bundle.compose_env(project="p", build=False, image="x")


def test_a_multi_line_value_cannot_inject_another_variable() -> None:
    with pytest.raises(ValueError):
        bundle.render_env({"A": "x\nEVIL=1"})


def test_the_rendered_adapter_configuration_loads_and_holds_no_secret(tmp_path: Path) -> None:
    text = bundle.serve_config("a.example.com")
    path = tmp_path / "serve.json"
    path.write_text(text)
    config = load_serve_config(path)
    assert config.public_url == "https://a.example.com"
    assert (config.host, config.port) == ("0.0.0.0", 8080)  # inside the container's own namespace
    assert config.secrets_dir == Path("/run/secrets")
    assert not config.allow_private_networks and not config.allow_insecure_http
    assert set(json.loads(text)) == {"publicUrl", "listen", "limits", "secretsDir"}


# ---- installing ------------------------------------------------------------------------------
def test_install_writes_private_files_and_prepares_the_directories() -> None:
    target = FakeTarget()
    env = bundle.compose_env(project="p", build=True)
    bundle.install(target, env, bundle.serve_config("a.example.com"))

    assert target.files[".env"].mode == 0o600
    assert target.files["config/serve.json"].mode == 0o644
    for script in bundle.SCRIPTS:
        assert target.files[script].mode == 0o755
    prepare = next(s for s in target.scripts if "mkdir -p config data secrets" in s)
    assert "chown 10001:10001 data secrets backups" in prepare
    assert "chmod 700 data secrets backups ts-state" in prepare


def test_units_are_installed_and_enabled_and_idle_stop_is_opt_in() -> None:
    target = FakeTarget()
    bundle.install_units(target)
    enable = next(s for s in target.scripts if "systemctl enable" in s)
    assert "muse-adapter-heal.timer" in enable and "muse-adapter-backup.timer" in enable
    assert "idle" not in enable.split("; ")[0] and "idle.env" not in target.files
    # Enabled alone would leave them idle until the next reboot: they must be started too.
    assert "systemctl start muse-adapter-backup.timer muse-adapter-heal.timer" in enable

    target = FakeTarget()
    bundle.install_units(target, idle_hours=6)
    assert "muse-adapter-idle.timer" in next(s for s in target.scripts if "systemctl enable" in s)
    assert target.text("idle.env") == "MMA_IDLE_HOURS=6\n"


def test_a_secret_is_stored_for_the_adapter_alone_and_never_appears_in_a_command() -> None:
    runner = FakeRunner()
    target = SSHTarget(runner, "me@host")  # type: ignore[arg-type]
    bundle.set_secret(target, "LINEAR_KEY", "lin_super_secret")

    assert not any("lin_super_secret" in line for line in runner.lines())  # not in any argument
    assert runner.calls[-1]["input"] == "lin_super_secret\n"  # it went over stdin
    script = runner.calls[-1]["argv"][-1]
    assert "chmod 400" in script and "chown 10001:10001" in script


@pytest.mark.parametrize("name", ["../x", "a/b", "1abc", "", "with space", "x;rm"])
def test_secret_names_that_could_escape_the_directory_are_refused(name: str) -> None:
    with pytest.raises(SystemExit):
        bundle.set_secret(FakeTarget(), name, "value")


@pytest.mark.parametrize("value", ["", "two\nlines"])
def test_a_secret_must_be_one_non_empty_line(value: str) -> None:
    with pytest.raises(SystemExit):
        bundle.set_secret(FakeTarget(), "NAME", value)


def test_the_source_upload_holds_exactly_what_the_images_need() -> None:
    runner = FakeRunner()
    target = SSHTarget(runner, "me@host")  # type: ignore[arg-type]
    bundle.upload_source(target, ROOT)
    upload = next(call for call in runner.calls if call.get("input"))
    with tarfile.open(fileobj=io.BytesIO(upload["input"]), mode="r:gz") as archive:
        names = archive.getnames()
        members = archive.getmembers()

    assert {"Dockerfile", "requirements.lock", "guard/Dockerfile", "guard/guard.sh"} <= set(names)
    assert "src/muse_mcp_adapter/cli.py" in names
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)
    assert not any(
        n.startswith(("tests", ".internal", ".git", "docs", "src/muse_vm")) for n in names
    )
    for member in members:
        assert member.uid == 0 and member.gid == 0
        assert member.mode & 0o044 == 0o044, member.name  # readable by the unprivileged user


def test_the_source_directory_is_found_from_a_checkout() -> None:
    assert bundle.find_source_dir(ROOT / "src" / "muse_vm") == ROOT


def test_upgrading_moves_built_images_but_leaves_ones_the_owner_chose() -> None:
    target = FakeTarget()
    built = {
        "MMA_VERSION": "0.0.9",
        "MMA_IMAGE": "muse-adapter-local/adapter:0.0.9",
        "MMA_GUARD_IMAGE": "muse-adapter-local/guard:0.0.9",
    }
    target.put(".env", bundle.render_env(built))
    assert bundle.set_version(target) == "0.0.9"
    env = bundle.parse_env(target.text(".env"))
    assert env["MMA_IMAGE"] == f"muse-adapter-local/adapter:{bundle.__version__}"
    assert bundle.set_version(target) is None  # already current

    own = {
        "MMA_VERSION": "0.0.9",
        "MMA_IMAGE": "registry.example.com/adapter@sha256:" + "a" * 64,
        "MMA_GUARD_IMAGE": "registry.example.com/guard@sha256:" + "b" * 64,
    }
    target.put(".env", bundle.render_env(own))
    bundle.set_version(target)
    after = bundle.parse_env(target.text(".env"))
    assert (
        after["MMA_IMAGE"] == own["MMA_IMAGE"]
        and after["MMA_GUARD_IMAGE"] == own["MMA_GUARD_IMAGE"]
    )


def test_the_source_revision_names_the_commit_and_notices_edits(tmp_path: Path) -> None:
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    git("add", "-A")
    git("commit", "-qm", "x")
    revision, dirty = bundle.source_revision(tmp_path)
    assert len(revision) == 12 and not dirty
    (tmp_path / "Dockerfile").write_text("FROM scratch\n# edited\n")
    assert bundle.source_revision(tmp_path)[1] is True
    assert bundle.source_revision(tmp_path / "missing") == ("unknown", False)


def test_every_bundle_file_ships_in_the_package() -> None:
    shipped = {item.name for item in resources.files("muse_vm").joinpath("bundle").iterdir()}
    assert set(bundle.STATIC_FILES + bundle.SCRIPTS + bundle.UNITS) <= shipped


# ---- systemd units ---------------------------------------------------------------------------
def _unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string((BUNDLE / name).read_text())
    return parser


def test_every_service_unit_runs_a_script_that_exists() -> None:
    for name in bundle.UNITS:
        if not name.endswith(".service"):
            continue
        parser = _unit(name)
        command = parser["Service"]["ExecStart"]
        for token in command.split():
            if token.startswith("/opt/muse-adapter/"):
                assert token.rsplit("/", 1)[-1] in bundle.SCRIPTS, f"{name}: {token}"


def test_the_helper_units_run_with_reduced_privileges() -> None:
    for name in (
        "muse-adapter-backup.service",
        "muse-adapter-heal.service",
        "muse-adapter-idle.service",
    ):
        assert _unit(name)["Service"]["NoNewPrivileges"] == "true"


def test_backups_run_daily_and_catch_up_after_downtime() -> None:
    timer = _unit("muse-adapter-backup.timer")["Timer"]
    assert timer["OnCalendar"] == "daily" and timer["Persistent"] == "true"


# ---- the shell scripts, run for real ---------------------------------------------------------
def _fake_docker(directory: Path) -> Path:
    bin_dir = directory / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'echo "$@" >> "$FAKE_DOCKER_LOG"\n'
        'case "$1 $2" in\n'
        '  "compose ps") [ -n "${FAKE_CONTAINER-}" ] && echo "$FAKE_CONTAINER" ;;\n'
        '  "inspect --format")\n'
        '    case "$3" in\n'
        '      *State.Status*) echo "${FAKE_STATUS:-running}" ;;\n'
        '      *) echo "${FAKE_HEALTH:-healthy}" ;;\n'
        "    esac ;;\n"
        "esac\n"
    )
    docker.chmod(0o755)
    return bin_dir


def _run_heal(tmp_path: Path, **fake: str) -> list[str]:
    bin_dir = _fake_docker(tmp_path)
    log = tmp_path / "docker.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "MMA_ROOT": str(tmp_path),
        "FAKE_DOCKER_LOG": str(log),
        **fake,
    }
    done = subprocess.run(["sh", str(BUNDLE / "heal.sh")], env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return log.read_text().splitlines() if log.exists() else []


def test_an_unhealthy_adapter_is_restarted(tmp_path: Path) -> None:
    calls = _run_heal(tmp_path, FAKE_CONTAINER="abc123", FAKE_HEALTH="unhealthy")
    assert "compose restart adapter" in calls


def test_a_healthy_adapter_is_left_alone(tmp_path: Path) -> None:
    calls = _run_heal(tmp_path, FAKE_CONTAINER="abc123", FAKE_HEALTH="healthy")
    assert not any("restart" in call for call in calls)


def test_a_missing_or_stopped_adapter_is_not_started_by_the_healer(tmp_path: Path) -> None:
    assert not any("restart" in c for c in _run_heal(tmp_path))
    stopped = tmp_path / "stopped"
    stopped.mkdir()
    calls = _run_heal(stopped, FAKE_CONTAINER="abc", FAKE_STATUS="exited", FAKE_HEALTH="unhealthy")
    assert not any("restart" in call for call in calls)  # stopped on purpose (muse-vm stop)


def _run_idle(tmp_path: Path, *, uptime: int, idle_seconds: int, hours: int = 2) -> bool:
    data = tmp_path / "data"
    data.mkdir()
    database = data / "adapter.db"
    database.write_text("x")
    modified = time.time() - idle_seconds
    os.utime(database, (modified, modified))
    up = tmp_path / "uptime"
    up.write_text(f"{uptime}.50 123.4\n")
    marker = tmp_path / "powered-off"
    env = {
        **os.environ,
        "MMA_IDLE_HOURS": str(hours),
        "MMA_DATA_DIR": str(data),
        "MMA_UPTIME_FILE": str(up),
        "MMA_POWEROFF_CMD": f"touch {marker}",
    }
    done = subprocess.run(["sh", str(BUNDLE / "idle.sh")], env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return marker.exists()


def test_an_idle_vm_powers_off(tmp_path: Path) -> None:
    assert _run_idle(tmp_path, uptime=10 * 3600, idle_seconds=3 * 3600) is True


def test_a_recently_used_vm_stays_on(tmp_path: Path) -> None:
    assert _run_idle(tmp_path, uptime=10 * 3600, idle_seconds=600) is False


def test_a_vm_that_just_started_stays_on_even_with_an_old_database(tmp_path: Path) -> None:
    assert _run_idle(tmp_path, uptime=600, idle_seconds=10 * 3600) is False


def test_the_guard_refuses_a_malformed_allow_list_before_touching_the_firewall() -> None:
    done = subprocess.run(
        ["sh", str(ROOT / "deploy" / "guard" / "guard.sh")],
        env={**os.environ, "MMA_EGRESS_ALLOW": "10.0.0.0/8; rm -rf /"},
        capture_output=True,
        text=True,
    )
    assert done.returncode == 1 and "not a CIDR" in done.stderr


def test_the_guard_blocks_the_metadata_and_private_ranges() -> None:
    text = (ROOT / "deploy" / "guard" / "guard.sh").read_text()
    for cidr in (
        "169.254.0.0/16",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "fc00::/7",
    ):
        assert cidr in text


def test_bootstrap_installs_updates_and_log_limits_and_only_on_apt_systems() -> None:
    text = (BUNDLE / "bootstrap.sh").read_text()
    assert "unattended-upgrades" in text and "20auto-upgrades" in text
    assert "max-size" in text and "max-file" in text
    assert "command -v apt-get" in text  # refuses cleanly elsewhere


def test_every_host_script_is_valid_shell_and_executable_in_the_bundle() -> None:
    for name in [*bundle.SCRIPTS, "bootstrap.sh"]:
        assert subprocess.run(["sh", "-n", str(BUNDLE / name)]).returncode == 0, name
    assert subprocess.run(["sh", "-n", str(ROOT / "deploy" / "guard" / "guard.sh")]).returncode == 0
    for name in [*bundle.SCRIPTS, "bootstrap.sh"]:
        assert stat.S_IMODE((BUNDLE / name).stat().st_mode) & 0o111, name


def test_the_tailscale_auth_key_is_forgotten_once_the_device_has_joined() -> None:
    target = FakeTarget()
    env = bundle.compose_env(
        project="p",
        build=True,
        ingress_secrets={"TS_AUTHKEY": "tskey-auth-kExample-aaaaaaaaaaaaaaaa"},
    )
    bundle.install(target, env, bundle.serve_config("a.example.com"))
    assert "TS_AUTHKEY" in target.text(".env")
    assert bundle.forget_auth_key(target) is True
    assert "TS_AUTHKEY" not in target.text(".env") and "tskey" not in target.text(".env")
    assert target.files[".env"].mode == 0o600
    assert bundle.forget_auth_key(target) is False  # nothing left to remove


# ---- what the bootstrap changes on a host -----------------------------------------------------
def _run_bootstrap(tmp_path: Path, *, docker_present: bool, updates_present: bool) -> list[str]:
    """Run the real bootstrap with stand-ins for the system commands; returns what was called."""
    fake = tmp_path / "bin"
    fake.mkdir()
    log = tmp_path / "calls.log"
    prefix = str(tmp_path / "root")
    for name in ("apt-get", "systemctl", "python3"):
        (fake / name).write_text(f'#!/bin/sh\necho "{name} $*" >> {log}\n')
    (fake / "docker").write_text(
        f'#!/bin/sh\necho "docker $*" >> {log}\n'
        + ("exit 0\n" if docker_present else f"[ -e {tmp_path}/docker-installed ] || exit 1\n")
    )
    (fake / "dpkg").write_text(f"#!/bin/sh\nexit {0 if updates_present else 1}\n")
    # "apt-get install docker.io" is what makes docker appear on a fresh host
    (fake / "apt-get").write_text(
        f'#!/bin/sh\necho "apt-get $*" >> {log}\n'
        f'case "$*" in *docker.io*) touch {tmp_path}/docker-installed;; esac\n'
    )
    for stub in fake.iterdir():
        stub.chmod(0o755)
    script = (BUNDLE / "bootstrap.sh").read_text()
    script = script.replace("/etc/", f"{prefix}/etc/").replace("/var/lib/", f"{prefix}/var/lib/")
    (tmp_path / "root" / "etc" / "apt" / "apt.conf.d").mkdir(parents=True, exist_ok=True)
    (tmp_path / "root" / "var" / "lib").mkdir(parents=True, exist_ok=True)
    path = tmp_path / "bootstrap.sh"
    path.write_text(script)
    done = subprocess.run(
        ["sh", str(path)],
        env={"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    return log.read_text().splitlines() if log.exists() else []


def test_a_host_that_already_runs_docker_is_not_touched(tmp_path: Path) -> None:
    calls = _run_bootstrap(tmp_path, docker_present=True, updates_present=True)
    assert not any("apt-get" in c or "systemctl" in c or c.startswith("python3") for c in calls)
    assert not (tmp_path / "root" / "etc" / "docker").exists()  # daemon.json is left alone
    assert not (tmp_path / "root" / "etc" / "apt" / "apt.conf.d" / "20auto-upgrades").exists()
    assert (tmp_path / "root" / "var" / "lib" / "muse-adapter" / "bootstrap-done").exists()


def test_an_existing_update_policy_is_never_overwritten(tmp_path: Path) -> None:
    (tmp_path / "root" / "etc" / "apt" / "apt.conf.d").mkdir(parents=True)
    custom = tmp_path / "root" / "etc" / "apt" / "apt.conf.d" / "20auto-upgrades"
    custom.write_text("// the owner's own policy\n")
    # unattended-upgrades missing but a policy file exists: install the package, keep the file
    calls = _run_bootstrap(tmp_path, docker_present=True, updates_present=False)
    assert any("install -y -q unattended-upgrades" in c for c in calls)
    assert custom.read_text() == "// the owner's own policy\n"


def test_a_fresh_host_gets_docker_updates_and_log_limits(tmp_path: Path) -> None:
    calls = _run_bootstrap(tmp_path, docker_present=False, updates_present=False)
    assert any("install -y -q docker.io" in c for c in calls)
    assert any("install -y -q unattended-upgrades" in c for c in calls)
    assert any(c.startswith("python3") for c in calls)  # daemon.json log limits
    assert "systemctl restart docker" in calls  # nothing runs on a fresh host to disturb
    assert (tmp_path / "root" / "etc" / "apt" / "apt.conf.d" / "20auto-upgrades").exists()
