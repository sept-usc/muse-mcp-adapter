"""`muse-vm setup`: from nothing to a running adapter with servers connected, in one guided run.

It asks only for what it cannot find out: where to run, which servers, and the tokens those
servers need. Everything else has a default and can be set in muse-vm.toml or with flags. Each
step is the ordinary command (`up`, `add-server`), so nothing here does anything a user could not
do by hand.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import browser, config, doctor, presets
from .providers.gcp import DEFAULT_ZONE, MONTHLY_USD
from .runner import Runner
from .selfcheck import Report
from .state import Deployment

DEFAULT_MACHINE = "e2-small"


@dataclass
class Io:
    """The terminal, so tests can drive the wizard without one."""

    say: Callable[[str], None]
    ask: Callable[[str], str]
    interactive: bool
    verbose: bool = False

    def confirm(self, question: str, default: bool = True) -> bool:
        for _ in range(3):
            answer = self.ask(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self.say("  Please answer y or n.")
        return default


def ask_at_terminal(question: str) -> str:
    """input(), after discarding anything typed while the wizard was busy (an Enter pressed to
    see if it was still working must not answer the next question)."""
    try:
        import termios

        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except (ImportError, OSError, ValueError):  # not a terminal, or no termios (Windows)
        pass
    return input(question)


def _read_secret_file(path: str) -> str:
    text = Path(path).expanduser().read_text(encoding="utf-8").strip()
    if not text or "\n" in text:
        raise SystemExit(f"{path} must hold a single non-empty line")
    return text


def _where(plan: config.Plan, io: Io, runner: Runner) -> None:
    """Fill in cloud or ssh: from the config, from what gcloud already knows, or by asking."""
    if plan.cloud or plan.ssh or plan.local:
        return
    if not io.interactive:
        raise SystemExit(
            'Say where to run the adapter: `cloud = "gcp"` or `ssh = "user@host"` in '
            "muse-vm.toml, or --cloud gcp / --ssh user@host."
        )
    facts = doctor.gcloud_facts(runner)
    google = facts is not None and facts.credentials_ok and facts.project is not None
    on_linux = sys.platform.startswith("linux") and not os.environ.get("CLOUD_SHELL")
    options: list[tuple[str, str]] = []
    if google and facts is not None:
        cost = MONTHLY_USD.get(plan.machine_type or DEFAULT_MACHINE)
        options.append(
            (
                "gcp",
                "A new VM in Google Cloud"
                + (f" (about ${cost}/month plus a little for disk and NAT)" if cost else ""),
            )
        )
    if on_linux:
        options.append(("local", "This machine (I am on the Linux VM the adapter should run on)"))
    options.append(("ssh", "A Linux machine I already have, over SSH"))
    io.say("Where should the adapter run?")
    if not google:
        reason = (
            "gcloud is not installed or signed in"
            if facts is None or not facts.credentials_ok
            else "gcloud has no project set"
        )
        io.say(f"  (Google Cloud is not available here: {reason}.)")
    for number, (_, label) in enumerate(options, 1):
        io.say(f"  {number}  {label}")
    answer = io.ask(f"Choose 1-{len(options)} [1] ").strip() or "1"
    if not answer.isdigit() or not 0 < int(answer) <= len(options):
        raise SystemExit("Please choose one of the options shown.")
    choice = options[int(answer) - 1][0]
    if choice == "gcp" and facts is not None:
        plan.cloud = "gcp"
    elif choice == "local":
        plan.local = True
    else:
        plan.ssh = io.ask("SSH address, for example deploy@203.0.113.7: ").strip()
        if not plan.ssh:
            raise SystemExit("An SSH address is needed.")


_ZONE = re.compile(r"^[a-z]+-[a-z]+[0-9]+-[a-z]$")
_MACHINE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)+$")
_PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


def _ask_valid(
    io: Io, prompt: str, default: str | None, pattern: re.Pattern[str], example: str
) -> str:
    for _ in range(3):
        suffix = f" [{default}] " if default else " "
        answer = io.ask(prompt + suffix).strip() or (default or "")
        if pattern.match(answer):
            return answer
        io.say(f"  That does not look right. For example: {example}")
    raise SystemExit(f"No valid answer for: {prompt}")


def _gcp_settings(plan: config.Plan, io: Io, runner: Runner) -> None:
    """Show the project, zone and machine setup would use, and let the person choose others.

    Nothing about where the VM goes is assumed silently: gcloud's own settings are offered as the
    default, and a settings file or flags that already name the project and zone skip the question.
    """
    if plan.cloud != "gcp" or not io.interactive or (plan.project and plan.zone):
        return
    facts = doctor.gcloud_facts(runner)
    project = plan.project or (facts.project if facts else None)
    zone = plan.zone or (facts.zone if facts else None)
    machine = plan.machine_type or DEFAULT_MACHINE
    cost = MONTHLY_USD.get(machine)
    io.say("")
    io.say("Google Cloud settings:")
    io.say(f"  project   {project or 'none set in gcloud'}")
    io.say(
        f"  zone      {zone or DEFAULT_ZONE}"
        + ("" if zone else "  (a default: gcloud has no zone set)")
    )
    io.say(f"  machine   {machine}" + (f"  (about ${cost}/month)" if cost else ""))
    if project and io.confirm("Use these settings?"):
        plan.project, plan.zone, plan.machine_type = project, zone or DEFAULT_ZONE, machine
        return
    listed = runner(
        ["gcloud", "projects", "list", "--format=value(projectId)", "--limit=15"], check=False
    )
    if listed.ok and listed.stdout.split():
        io.say("Your projects: " + ", ".join(listed.stdout.split()))
    plan.project = _ask_valid(io, "Project ID", project, _PROJECT, "my-project-123456")
    plan.zone = _ask_valid(io, "Zone", zone or DEFAULT_ZONE, _ZONE, "us-central1-a, europe-west4-b")
    io.say(
        "Machine types: "
        + ", ".join(f"{name} (about ${price}/month)" for name, price in MONTHLY_USD.items())
        + ", or any other Compute Engine type"
    )
    plan.machine_type = _ask_valid(io, "Machine type", machine, _MACHINE, "e2-small, e2-medium")


def _checks(plan: config.Plan, io: Io, runner: Runner) -> None:
    report = Report()
    doctor.local_checks(report)
    if plan.local:
        doctor.local_machine_checks(report, runner)
    elif plan.ssh:
        doctor.ssh_checks(report, runner, plan.ssh, plan.ssh_opts)
    else:
        doctor.gcp_checks(report, runner, plan.project, plan.zone)
    lines = report.lines()
    if report.ok and not io.verbose:
        io.say("  ✓ Everything needed is in place")
        lines = [line for line in lines if not line.lstrip().startswith("✓")]  # notes still show
    for line in lines:
        io.say(line)
    if not report.ok:
        raise SystemExit("Fix the items marked ✗ above and run `muse-vm setup` again.")


def _up_argv(plan: config.Plan, name: str) -> list[str]:
    argv = ["up", "--name", name, "--yes"]
    if plan.local:
        argv.append("--local")
    elif plan.ssh:
        argv += ["--ssh", plan.ssh]
    else:
        argv += ["--cloud", "gcp"]
    for flag, value in (
        ("--project", plan.project),
        ("--zone", plan.zone),
        ("--machine-type", plan.machine_type),
        ("--budget-usd", plan.budget_usd),
        ("--idle-stop-hours", plan.idle_stop_hours),
        ("--tz", plan.tz),
    ):
        if value is not None:
            argv += [flag, str(value)]
    if plan.egress_allow:
        argv += ["--egress-allow", ",".join(plan.egress_allow)]
    for option in plan.ssh_opts:
        argv.append(f"--ssh-opt={option}")
    return argv


def _own_server(url: str, io: Io) -> config.ServerPlan:
    """A server that is not in the list, given by its address."""
    host = urlsplit(url).hostname or ""
    slug = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")
    if not slug:
        raise ValueError(f"{url!r} is not an address")
    plan = config.ServerPlan(server=slug, endpoint=url, credential="none")
    if io.confirm(f"Does {host} need a token?", default=False):
        plan.credential = "env_token"
        plan.secret_name = slug.upper().replace("-", "_") + "_TOKEN"
    return plan


def _choose_servers(io: Io) -> list[config.ServerPlan]:
    io.say("")
    io.say("Which MCP servers should Muse be able to use? (you can add more later)")
    names = list(presets.PRESETS)
    for number, line in enumerate(presets.describe(), 1):
        io.say(f"  {number}  {line}")
    io.say("  or paste the address of your own server, such as https://mcp.example.com/mcp")
    for _ in range(3):
        answer = io.ask("Numbers, names or addresses, separated by commas (Enter to skip): ")
        chosen: list[config.ServerPlan] = []
        unknown: list[str] = []
        for part in (item.strip() for item in answer.replace(" ", ",").split(",")):
            if not part:
                continue
            if part.lower().startswith(("http://", "https://")):
                try:
                    chosen.append(_own_server(part, io))
                except ValueError:
                    unknown.append(part)
                continue
            name = names[int(part) - 1] if part.isdigit() and 0 < int(part) <= len(names) else part
            if presets.find(name) is None:
                unknown.append(part)
            else:
                chosen.append(config.ServerPlan(server=name))
        if not unknown:
            return chosen
        io.say(f"  Not in the list: {', '.join(unknown)}. Try again, or press Enter to skip.")
    return []


def _connect(
    server: config.ServerPlan, name: str, run_command: Callable[[list[str]], int], io: Io
) -> bool:
    preset = presets.find(server.server)
    secret = server.secret_name or (preset.secret_name if preset else None)
    if server.token_file:
        if not secret:
            raise SystemExit(
                f"{server.server}: token_file needs secret_name (or a well-known server)"
            )
        os.environ[secret] = _read_secret_file(server.token_file)
    argv = ["add-server", server.server, "--name", name, "--yes"]
    for flag, value in (
        ("--endpoint", server.endpoint),
        ("--credential", server.credential),
        ("--secret-name", server.secret_name),
    ):
        if value:
            argv += [flag, value]
    for tool in server.allow:
        argv += ["--allow", tool]
    if server.no_reads:
        argv.append("--no-reads")
    io.say("")
    io.say(f"Connecting {preset.name if preset else server.server}…")
    return run_command(argv) == 0


def run(
    plan: config.Plan,
    *,
    name: str,
    runner: Runner,
    run_command: Callable[[list[str]], int],
    io: Io,
    save_to: Path | None = None,
    config_existed: bool = False,
) -> None:
    io.say("muse-vm setup: you will be asked only for what cannot be worked out.")
    io.say("")
    _where(plan, io, runner)
    _gcp_settings(plan, io, runner)
    if plan.cloud and not plan.yes:
        cost = MONTHLY_USD.get(plan.machine_type or DEFAULT_MACHINE)
        if not io.interactive:
            raise SystemExit(
                "This creates billable cloud resources: add `yes = true` to muse-vm.toml, "
                "or pass --yes."
            )
        if not io.confirm(
            f"This creates a VM in project {plan.project}"
            + (f" (about ${cost}/month while running)" if cost else "")
            + ". Continue?"
        ):
            raise SystemExit("Nothing was created.")
        plan.yes = True

    io.say("")
    io.say("Checking this computer and the target…")
    _checks(plan, io, runner)

    if plan.tailscale_authkey_file:
        os.environ["TS_AUTHKEY"] = _read_secret_file(plan.tailscale_authkey_file)

    existing = Deployment.path_for(name).exists() and Deployment.load(name).status == "ready"
    if existing:
        io.say("")
        io.say(f"The deployment {name!r} already exists and is ready: keeping it.")
    else:
        io.say("")
        if "TS_AUTHKEY" in os.environ:
            io.say("Creating the deployment (about 5 to 10 minutes: it builds the images)…")
        else:
            opens = browser.interactive()
            io.say(
                "Creating the deployment (about 5 to 10 minutes). Tailscale gives the adapter its\n"
                "public address: you will be shown up to two links (a sign-in and an approval)"
                + (" and they\nopen in your browser." if opens else ".")
                + " Click approve in each."
            )
        code = run_command(_up_argv(plan, name))
        if code != 0:
            raise SystemExit(code)

    servers = plan.servers
    if not servers and io.interactive:
        servers = _choose_servers(io)
        plan.servers = servers
    failed = [s for s in servers if not _connect(s, name, run_command, io)]

    if save_to is not None and (
        not config_existed
        and io.interactive
        and io.confirm(f"Save these choices to {save_to.name}?")
    ):
        save_to.write_text(config.dump(plan), encoding="utf-8")
        io.say(f"Saved {save_to}. Next time: muse-vm setup (it reads that file).")

    dep = Deployment.load(name) if Deployment.path_for(name).exists() else None
    io.say("")
    io.say(
        "Done."
        if not failed
        else "The adapter is installed and running, but not everything connected."
    )
    if dep and dep.base_url:
        io.say(f"  The adapter is running at {dep.base_url}")
    for server in failed:
        io.say(
            f"  Not connected: {server.server}. Fix the reason shown above (often a rejected or "
            f"expired token), then run: muse-vm add-server {server.server} --name {name}"
        )
    io.say("  Add a server to Muse: tell Muse the connector address printed above, click Connect,")
    io.say(
        "  and paste the token. More servers: muse-vm add-server <name>. Everything: muse-vm --help"
    )
    if failed:
        raise SystemExit(1)
