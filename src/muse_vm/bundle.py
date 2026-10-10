"""The deployment bundle: render it, install it on a target, and look after its secrets.

Layout on the target, in /opt/muse-adapter:

  compose.yml, compose.build.yml   the services (static)
  tailscale-serve.json             what Tailscale Funnel publishes (static)
  .env                  compose variables: images, tailscale settings           (0600)
  config/serve.json     the adapter's configuration; names no secret
  data/                 the adapter's database, key file and token store        (0700, uid 10001)
  secrets/              one file per secret an MCP server needs                 (0700, files 0400)
  backups/              backups the adapter makes of its own state              (0700, uid 10001)
  *.sh                  backup, self-heal and idle scripts run by systemd timers
"""

from __future__ import annotations

import io
import json
import re
import shlex
import subprocess
import tarfile
from importlib import resources
from pathlib import Path

from . import __version__
from .targets import Target

UID = 10001
STATIC_FILES = [
    "compose.yml",
    "compose.build.yml",
    "tailscale-serve.json",
]
SCRIPTS = ["backup.sh", "heal.sh", "idle.sh"]
UNITS = [
    "muse-adapter.service",
    "muse-adapter-backup.service",
    "muse-adapter-backup.timer",
    "muse-adapter-heal.service",
    "muse-adapter-heal.timer",
    "muse-adapter-idle.service",
    "muse-adapter-idle.timer",
]
# Muse reaches the adapter through Tailscale Funnel: no public IP, no open port, no domain.
INGRESS = "tailscale-funnel"
INGRESS_SERVICE = "tailscale"
SECRET_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def bundle_file(name: str) -> str:
    return resources.files("muse_vm").joinpath("bundle", name).read_text(encoding="utf-8")


def local_images() -> list[str]:
    """The image tags `--build` produces (see compose.build.yml)."""
    return [
        f"muse-adapter-local/adapter:{__version__}",
        f"muse-adapter-local/guard:{__version__}",
    ]


# ---- configuration --------------------------------------------------------------------------
def parse_env(text: str | None) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value
    return values


def render_env(values: dict[str, str]) -> str:
    for key, value in values.items():
        if "\n" in value:
            raise ValueError(f"{key}: a value must be a single line")
    return "".join(f"{key}={value}\n" for key, value in values.items())


def compose_env(
    *,
    project: str,
    build: bool,
    image: str | None = None,
    guard_image: str | None = None,
    tz: str = "UTC",
    egress_allow: str = "",
    ts_hostname: str | None = None,
    ingress_secrets: dict[str, str] | None = None,
) -> dict[str, str]:
    if not build and not (image and guard_image):
        raise SystemExit("Without building from source, give both --image and --guard-image.")
    files = ["compose.yml"]
    if build:
        files.append("compose.build.yml")
    local = local_images()
    env = {
        "MMA_PROJECT": project,
        "MMA_VERSION": __version__,
        "COMPOSE_FILE": ":".join(files),
        "MMA_IMAGE": local[0] if build else str(image),
        "MMA_GUARD_IMAGE": local[1] if build else str(guard_image),
        "MMA_TZ": tz,
        "MMA_EGRESS_ALLOW": egress_allow,
    }
    if ts_hostname:
        env["MMA_TS_HOSTNAME"] = ts_hostname
    env.update(ingress_secrets or {})
    return env


def serve_config(hostname: str, *, rate_limit: int = 60) -> str:
    """The adapter's own configuration: it names where secrets are, and holds none."""
    return (
        json.dumps(
            {
                "publicUrl": f"https://{hostname}",
                "listen": {"host": "0.0.0.0", "port": 8080},
                "limits": {
                    "rateLimit": rate_limit,
                    "rateWindowSeconds": 60,
                    "perServerConcurrency": 8,
                },
                "secretsDir": "/run/secrets",
            },
            indent=2,
        )
        + "\n"
    )


# ---- installing on a target -----------------------------------------------------------------
def install(target: Target, env: dict[str, str], serve_json: str) -> None:
    """Write the bundle and create its directories with the right owners and modes."""
    for name in STATIC_FILES:
        target.put(name, bundle_file(name), 0o644)
    target.put(".env", render_env(env), 0o600)
    target.put("config/serve.json", serve_json, 0o644)
    for name in SCRIPTS:
        target.put(name, bundle_file(name), 0o755)
    target.sh(
        "mkdir -p config data secrets backups ts-state && chmod 755 . config "
        f"&& chown {UID}:{UID} data secrets backups && chmod 700 data secrets backups ts-state"
    )


def install_units(target: Target, *, idle_hours: int | None = None) -> None:
    """Install the systemd units: the service, backups, self-healing and optional idle stop."""
    for name in UNITS:
        target.put(f"units/{name}", bundle_file(name), 0o644)
    timers = ["muse-adapter-backup.timer", "muse-adapter-heal.timer"]
    if idle_hours:
        target.put("idle.env", f"MMA_IDLE_HOURS={int(idle_hours)}\n", 0o644)
        timers.append("muse-adapter-idle.timer")
    # Enabled so they survive a reboot, and started now so they run without one.
    script = (
        "install -m 644 units/*.service units/*.timer /etc/systemd/system/ "
        "&& systemctl daemon-reload "
        f"&& systemctl enable muse-adapter.service {' '.join(timers)} >/dev/null "
        f"&& systemctl start {' '.join(timers)}"
    )
    if not idle_hours:
        script += "; systemctl disable --now muse-adapter-idle.timer >/dev/null 2>&1 || true"
    target.sh(script)


def remove_units(target: Target) -> None:
    target.sh(
        "systemctl disable --now muse-adapter-idle.timer muse-adapter-heal.timer "
        "muse-adapter-backup.timer muse-adapter.service >/dev/null 2>&1; "
        "rm -f /etc/systemd/system/muse-adapter*.service /etc/systemd/system/muse-adapter*.timer; "
        "systemctl daemon-reload",
        check=False,
        cwd_root=False,
    )


def set_secret(
    target: Target, name: str, value: str, *, mode: int = 0o400, owner: str = f"{UID}:{UID}"
) -> None:
    """Store a secret an MCP server needs. It travels on stdin; only the adapter can read it."""
    if not SECRET_NAME.match(name):
        raise SystemExit(f"{name!r} is not a valid secret name (letters, digits and '_' only).")
    if not value or "\n" in value:
        raise SystemExit("A secret must be a single non-empty line.")
    target.put(f"secrets/{name}", value + "\n", mode, owner=owner)


def list_secrets(target: Target) -> list[str]:
    result = target.sh("ls -1 secrets 2>/dev/null || true", check=False)
    return sorted(line for line in result.stdout.split() if SECRET_NAME.match(line))


# ---- images ---------------------------------------------------------------------------------
def find_source_dir(start: Path | None = None) -> Path | None:
    """The checkout this package runs from (or the current directory is in), if there is one."""
    for origin in (start or Path(__file__).resolve().parent, Path.cwd()):
        for parent in (origin, *origin.parents):
            if (
                (parent / "Dockerfile").is_file()
                and (parent / "requirements.lock").is_file()
                and (parent / "deploy" / "guard" / "Dockerfile").is_file()
                and (parent / "src" / "muse_mcp_adapter").is_dir()
            ):
                return parent
    return None


def source_revision(source: Path) -> tuple[str, bool]:
    """The commit the checkout is at, and whether it has uncommitted changes."""

    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", "-C", str(source), *args], capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    revision = git("rev-parse", "--short=12", "HEAD")
    if not revision:
        return "unknown", False
    changed = git(
        "status",
        "--porcelain",
        "--",
        "Dockerfile",
        "requirements.lock",
        "src/muse_mcp_adapter",
        "deploy/guard",
    )
    return revision, bool(changed)


def upload_source(target: Target, source: Path) -> str:
    """Send what the images are built from: the adapter, its lock, and the egress guard.

    Returns the commit they were built from, which is recorded in the images.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for relative, arcname in (
            ("Dockerfile", "Dockerfile"),
            ("requirements.lock", "requirements.lock"),
            ("src/muse_mcp_adapter", "src/muse_mcp_adapter"),
            ("deploy/guard", "guard"),
        ):
            archive.add(source / relative, arcname=arcname, filter=_normalise)
    target.sh("rm -rf src && mkdir -p src && tar -xpzf - -C src", input=buffer.getvalue())
    revision, dirty = source_revision(source)
    revision += "-modified" if dirty else ""
    env = parse_env(target.get(".env"))
    if env:
        env["MMA_REVISION"] = revision
        target.put(".env", render_env(env), 0o600)
    return revision


def _normalise(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    if "__pycache__" in info.name or info.name.endswith(".pyc"):
        return None
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    # Files end up root-owned in the image while the adapter runs unprivileged, so they must be
    # readable by everyone even if the checkout is not.
    if info.isdir():
        info.mode = 0o755
    elif info.isfile():
        info.mode = 0o755 if info.mode & 0o100 else 0o644
    return info


def set_version(target: Target) -> str | None:
    """On upgrade: move locally built images to this version. Returns the version replaced."""
    env = parse_env(target.get(".env"))
    previous = env.get("MMA_VERSION")
    if not env or previous == __version__:
        return None
    env["MMA_VERSION"] = __version__
    local = local_images()
    if env.get("MMA_IMAGE", "").startswith("muse-adapter-local/"):
        env["MMA_IMAGE"], env["MMA_GUARD_IMAGE"] = local
    target.put(".env", render_env(env), 0o600)
    return previous


def set_hostname(target: Target, hostname: str) -> bool:
    """Record the public hostname once Tailscale has assigned it. True if it changed."""
    wanted = serve_config(hostname)
    if target.get("config/serve.json") == wanted:
        return False
    target.put("config/serve.json", wanted, 0o644)
    return True


def ingress_logs(target: Target, tail: int = 200) -> str:
    result = target.compose(
        f"logs --no-color --tail {tail} {shlex.quote(INGRESS_SERVICE)}", check=False
    )
    return result.stdout + result.stderr
