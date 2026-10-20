"""`muse-vm setup`: from nothing to a running adapter with servers connected, in one guided run.

It asks only for what it cannot find out: where to run, which servers, and the tokens those
servers need. Everything else has a default and can be set in muse-vm.toml or with flags. Each
step is the ordinary command (`up`, `add-server`), so nothing here does anything a user could not
do by hand.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import browser, config, doctor, presets
from .providers.gcp import MONTHLY_USD
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

    def confirm(self, question: str, default: bool = True) -> bool:
        answer = self.ask(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
        return default if not answer else answer in ("y", "yes")


def _read_secret_file(path: str) -> str:
    text = Path(path).expanduser().read_text(encoding="utf-8").strip()
    if not text or "\n" in text:
        raise SystemExit(f"{path} must hold a single non-empty line")
    return text


def _where(plan: config.Plan, io: Io, runner: Runner) -> None:
    """Fill in cloud or ssh: from the config, from what gcloud already knows, or by asking."""
    if plan.cloud or plan.ssh:
        return
    if not io.interactive:
        raise SystemExit(
            'Say where to run the adapter: `cloud = "gcp"` or `ssh = "user@host"` in '
            "muse-vm.toml, or --cloud gcp / --ssh user@host."
        )
    facts = doctor.gcloud_facts(runner)
    google = facts is not None and facts.credentials_ok and facts.project is not None
    io.say("Where should the adapter run?")
    if google and facts is not None:
        cost = MONTHLY_USD.get(plan.machine_type or DEFAULT_MACHINE)
        io.say(
            f"  1  A new VM in Google Cloud, project {facts.project}"
            + (f" (about ${cost}/month plus a little for disk and NAT)" if cost else "")
        )
        io.say("  2  A Linux machine I already have, over SSH")
        choice = io.ask("Choose 1 or 2 [1] ").strip() or "1"
    else:
        reason = (
            "gcloud is not installed or signed in"
            if facts is None or not facts.credentials_ok
            else "gcloud has no project set"
        )
        io.say(f"  (Google Cloud is not available here: {reason}.)")
        io.say("  1  A Linux machine I already have, over SSH")
        choice = "2" if (io.ask("Choose 1 [1] ").strip() or "1") == "1" else "x"
    if choice == "1" and google and facts is not None:
        plan.cloud, plan.project = "gcp", plan.project or facts.project
    elif choice == "2":
        plan.ssh = io.ask("SSH address, for example deploy@203.0.113.7: ").strip()
        if not plan.ssh:
            raise SystemExit("An SSH address is needed.")
    else:
        raise SystemExit("Please choose one of the options shown.")


def _checks(plan: config.Plan, io: Io, runner: Runner) -> None:
    report = Report()
    doctor.local_checks(report)
    if plan.ssh:
        doctor.ssh_checks(report, runner, plan.ssh, plan.ssh_opts)
    else:
        doctor.gcp_checks(report, runner, plan.project, plan.zone)
    for line in report.lines():
        io.say(line)
    if not report.ok:
        raise SystemExit("Fix the items marked ✗ above and run `muse-vm setup` again.")


def _up_argv(plan: config.Plan, name: str) -> list[str]:
    argv = ["up", "--name", name, "--yes"]
    if plan.ssh:
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


def _choose_servers(io: Io) -> list[config.ServerPlan]:
    io.say("")
    io.say("Which MCP servers should Muse be able to use? (you can add more later)")
    names = list(presets.PRESETS)
    for number, line in enumerate(presets.describe(), 1):
        io.say(f"  {number}  {line}")
    answer = io.ask("Numbers or names, separated by commas (Enter to skip): ").strip()
    chosen: list[config.ServerPlan] = []
    for part in (item.strip() for item in answer.replace(" ", ",").split(",")):
        if not part:
            continue
        name = names[int(part) - 1] if part.isdigit() and 0 < int(part) <= len(names) else part
        if presets.find(name) is None:
            raise SystemExit(f"{part!r} is not one of the servers listed.")
        chosen.append(config.ServerPlan(server=name))
    return chosen


def _connect(
    server: config.ServerPlan, name: str, run_command: Callable[[list[str]], int], io: Io
) -> None:
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
    code = run_command(argv)
    if code != 0:
        raise SystemExit(code)


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
            io.say("Creating the deployment (10 to 20 minutes: it builds the images)…")
        else:
            opens = browser.interactive()
            io.say(
                "Creating the deployment (10 to 20 minutes). Tailscale gives the adapter its\n"
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
    for server in servers:
        _connect(server, name, run_command, io)

    if save_to is not None and (
        not config_existed
        and io.interactive
        and io.confirm(f"Save these choices to {save_to.name}?")
    ):
        save_to.write_text(config.dump(plan), encoding="utf-8")
        io.say(f"Saved {save_to}. Next time: muse-vm setup (it reads that file).")

    dep = Deployment.load(name) if Deployment.path_for(name).exists() else None
    io.say("")
    io.say("Done.")
    if dep and dep.base_url:
        io.say(f"  The adapter is running at {dep.base_url}")
    io.say("  Add a server to Muse: tell Muse the connector address printed above, click Connect,")
    io.say(
        "  and paste the token. More servers: muse-vm add-server <name>. Everything: muse-vm --help"
    )
