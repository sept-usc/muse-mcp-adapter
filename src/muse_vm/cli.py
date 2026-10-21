"""muse-vm: put the Muse MCP Adapter on a VM and look after it from your own computer."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import (
    __version__,
    browser,
    bundle,
    config,
    crypt,
    doctor,
    ingress,
    presets,
    selfcheck,
)
from . import setup as wizard
from .admin import Admin, Linked, check_server_id, pick_exposure
from .providers import PROVIDERS, provider_for
from .providers.base import Provider
from .runner import CommandError, Runner
from .selfcheck import Report
from .state import Deployment, check_name

DEFAULT_NAME = "muse"
PENDING_NOTE = (
    "  Its public name has not reached public DNS yet. That is normal for a new name and can take "
    "up to 20 minutes; everything inside the machine is healthy. Check again with "
    "`muse-vm selfcheck --name {name}`, and connect Muse once it passes."
)


def say(message: str = "") -> None:
    print(message, flush=True)


# ---- small helpers ---------------------------------------------------------------------------
def _runner(args: argparse.Namespace) -> Runner:
    return Runner(verbose=getattr(args, "verbose", False))


def _pick(name: str | None) -> Deployment:
    if name:
        return Deployment.load(name)
    found = Deployment.all()
    if len(found) == 1:
        return found[0]
    if not found:
        raise SystemExit("There is no deployment yet. Create one with `muse-vm up`.")
    raise SystemExit(
        "There is more than one deployment; say which with --name: "
        + ", ".join(item.name for item in found)
    )


def _context(args: argparse.Namespace, *, ready: bool = True) -> tuple[Deployment, Provider]:
    dep = _pick(args.name)
    provider = provider_for(dep, _runner(args), say, {"yes": getattr(args, "yes", False)})
    if ready:
        provider.ensure_ready()
    return dep, provider


def _secret_value(variable: str, prompt: str, *, required: bool = True) -> str | None:
    """From the environment, else asked for without echo. Never taken from the command line."""
    value = os.environ.get(variable)
    if value:
        return value
    if sys.stdin.isatty():
        entered = getpass.getpass(f"  {prompt}: ").strip()
        if entered:
            return entered
    if required:
        raise SystemExit(
            f"{prompt} is needed: set {variable} in the environment or run in a terminal."
        )
    return None


def _confirm(question: str, assume_yes: bool) -> None:
    """Ask before a destructive step at a terminal; --yes or no terminal goes on."""
    if assume_yes or not sys.stdin.isatty():
        return
    if input(f"  {question} [y/N] ").strip().lower() not in ("y", "yes"):
        raise SystemExit("Aborted.")


def _copy(text: str) -> bool:
    for command in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        try:
            subprocess.run(command, input=text.encode(), check=True, capture_output=True)
            return True
        except (OSError, subprocess.CalledProcessError):
            continue
    return False


def _muse_sentence(linked: Linked) -> str:
    """The sentence to tell Muse: it never includes the token, so it is safe to copy."""
    return linked.muse_instructions.split(" If Muse")[0].strip()


def _show_linked(linked: Linked, *, copy: bool) -> None:
    say()
    say(f"  Connector URL    {linked.connector_url}")
    say(f"  Token            {linked.token}")
    say(f"  Connection code  {linked.connection_code}")
    say()
    say("  The token and the code are shown once; the adapter keeps only hashes of them.")
    say("  In a new Muse chat, send:")
    say(f"    {_muse_sentence(linked)}")
    say("  Then click Connect and paste the token, or sign in with the connection code.")
    if copy and _copy(_muse_sentence(linked)):
        say("  (that sentence is on your clipboard; it holds no secret)")


# ---- up ---------------------------------------------------------------------------------------
def cmd_up(args: argparse.Namespace) -> None:
    name = check_name(args.name)
    if Deployment.path_for(name).exists():
        earlier = Deployment.load(name)
        unfinished = (
            f"An earlier run named {name!r} did not finish (it is {earlier.status}). Remove what "
            f"it created with `muse-vm down --name {name}`; if the cloud resources are already "
            f"gone, `muse-vm down --name {name} --forget` clears the record."
        )
        raise SystemExit(
            unfinished
            if earlier.status != "ready"
            else f"A deployment named {name!r} already exists (`muse-vm status --name {name}`, "
            f"or `muse-vm down --name {name}` first)."
        )
    provider_name = "local" if args.local else "ssh" if args.ssh else args.cloud
    if not provider_name:
        raise SystemExit(
            "Choose where to run the adapter: --ssh user@host, --cloud gcp, or --local for this "
            "machine. (`muse-vm setup` asks you instead.)"
        )
    build = not args.image
    if args.image and not args.guard_image:
        raise SystemExit("--image needs --guard-image too: both images come from your registry.")
    source: bundle.BuildSource | None = None
    if build:
        source = bundle.find_build_source()
        if source is None:
            raise SystemExit(
                "There is nothing to build the images from: install muse-mcp-adapter from a "
                "release, or run muse-vm inside a checkout of the repository. (To use images "
                "from your own registry, pass --image and --guard-image.)"
            )
        _refuse_unreviewed(source, args.allow_dirty)
    dep = Deployment(
        name=name,
        provider=provider_name,
        ingress=bundle.INGRESS,
        image=args.image,
        build=build,
        ssh_host=args.ssh,
        ssh_opts=args.ssh_opt or [],
    )
    options = {
        "project": args.project,
        "zone": args.zone,
        "machine_type": args.machine_type,
        "budget_usd": args.budget_usd,
        "idle_stop_hours": args.idle_stop_hours,
        "snapshots": not args.no_snapshots,
        "yes": args.yes,
        "reuse_dir": args.force,
        "replace_leftovers": getattr(args, "replace_leftovers", False),
    }
    runner = _runner(args)
    provider = provider_for(dep, runner, say, options)
    say(f"muse-vm {__version__}: deploying {name!r} to {provider_name}")

    ingress_secrets: dict[str, str] = {}
    # With --yes (the guided setup, scripts) nobody is asked: a key comes from the environment,
    # otherwise the person signs in with a link.
    key = (
        os.environ.get("TS_AUTHKEY")
        if args.yes
        else _secret_value(
            "TS_AUTHKEY", "Tailscale auth key (empty: sign in with a link)", required=False
        )
    )
    if key:
        if not re.fullmatch(r"tskey-[A-Za-z0-9_-]{20,}", key):
            raise SystemExit(
                "That does not look like a Tailscale auth key (it should start with `tskey-` and "
                "hold no spaces). Generate one under Settings, Keys in the Tailscale console."
            )
        ingress_secrets["TS_AUTHKEY"] = key

    say("1/6 preflight")
    provider.preflight()
    dep.save()
    t = None
    joined = False  # the device has been signed in to Tailscale (and so exists in the tailnet)
    try:
        say("2/6 provision")
        t = provider.provision()
        say(f"3/6 install on {t.describe()}")
        env = bundle.compose_env(
            project=f"muse-adapter-{name}",
            build=build,
            image=args.image,
            guard_image=args.guard_image,
            tz=args.tz,
            egress_allow=args.egress_allow or "",
            ts_hostname=f"muse-adapter-{name}",
            ingress_secrets=ingress_secrets,
        )
        bundle.install(t, env, bundle.serve_config("pending.invalid"))
        _fetch_images(t, dep, env, source, say)
        say("4/6 start")
        if "TS_AUTHKEY" not in ingress_secrets:
            ingress.interactive_login(
                t, say=say, open_link=None if args.no_browser else browser.open_link
            )
        joined = True
        provider.start_stack(t)
        dep.hostname = ingress.hostname_after_start(
            t, say=say, open_link=None if args.no_browser else browser.open_link
        )
        dep.save()
        if bundle.set_hostname(t, dep.hostname):
            provider.restart_adapter(t)
        say(f"5/6 public address https://{dep.hostname}")
        say("6/6 self-check")
        report = selfcheck.run(t, str(dep.base_url), wait=args.wait or 600, say=say)
        for line in report.lines():
            say(line)
        if not report.ok:
            _show_logs(t)
            raise SystemExit("The self-check failed.")
        if report.address_pending:
            dep.extra["address_pending"] = True
        if bundle.forget_auth_key(t):
            say("  removing the Tailscale auth key from the machine (the device has joined)")
            t.compose("up -d --no-deps tailscale", stream=True)
        provider.after_start(t)
        dep.status = "ready"
        dep.save()
    except BaseException as error:
        dep.status = "failed"
        dep.save()
        if args.keep_on_failure:
            say(
                f"\n✗ {error or type(error).__name__}\nEverything was kept for debugging. "
                f"Remove it with `muse-vm down --name {name}`."
            )
            raise
        if isinstance(error, KeyboardInterrupt):
            say("\nInterrupted.")
            if _keep_after_interrupt(name):
                dep.status = "failed"
                dep.save()
                raise SystemExit(130) from None
            say("Rolling back…")
        else:
            say(f"\n✗ {error or type(error).__name__}\nRolling back…")
        try:
            provider.teardown()
            dep.delete()
            say("Rolled back: nothing this run created is left.")
            if joined:
                say(
                    f"Tailscale keeps the device you signed in (muse-adapter-{name}) until you "
                    "delete it under Machines in the Tailscale admin page. Delete it so the next "
                    "install keeps the name."
                )
            say("Fix the reason above and run `muse-vm setup` again (add -v to see every step).")
        except BaseException as problem:
            say(
                f"The rollback is incomplete: {problem}\n"
                f"The state is kept in {Deployment.path_for(name)}; "
                f"run `muse-vm down --name {name}`."
            )
        if isinstance(error, KeyboardInterrupt):
            raise
        raise SystemExit(1) from None  # the reason was printed above; do not print it twice
    say()
    say(f"The adapter is running at https://{dep.hostname}")
    if dep.extra.get("address_pending"):
        say(PENDING_NOTE.format(name=name))
    say("Next, connect an MCP server and link it to Muse:")
    say(f"  muse-vm add-server learn --endpoint https://learn.microsoft.com/api/mcp --name {name}")


def _keep_after_interrupt(name: str) -> bool:
    """After Ctrl-C, ask before throwing away what was built (it can take ten minutes).

    Returns True to keep it. Without a terminal, or on a second Ctrl-C, it rolls back as before.
    """
    if not sys.stdin.isatty():
        return False
    try:
        answer = input("  Remove everything created so far? [Y/n] ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return False
    if answer in ("n", "no"):
        say(
            f"Kept. See it with `muse-vm status --name {name}`, check it with `muse-vm selfcheck "
            f"--name {name}`, or remove it with `muse-vm down --name {name}`."
        )
        return True
    return False


def _refuse_unreviewed(source: bundle.BuildSource, allow: bool) -> None:
    """A deployment should come from a commit that can be named again, not from local edits."""
    if source.dirty and not allow:
        raise SystemExit(
            f"The checkout at {source.revision} has uncommitted changes to what is deployed. "
            "Commit or stash them so the deployment matches a commit, or pass --allow-dirty."
        )


def _fetch_images(
    t: Any, dep: Deployment, env: dict[str, str], source: bundle.BuildSource | None, log: Any
) -> None:
    if dep.build:
        assert source
        log(
            f"  uploading the source ({source.kind} {source.revision}) and building the images on "
            "the machine (a few minutes)…"
        )
        bundle.upload_source(t, source)
        t.compose("build", stream=True, timeout=3600)
        return
    try:
        t.compose("pull --quiet", stream=True, timeout=1800)
    except CommandError as error:
        raise SystemExit(
            f"The images could not be pulled ({env['MMA_IMAGE']}, {env['MMA_GUARD_IMAGE']}). "
            "Check the names and that the machine can sign in to your registry."
        ) from error


def _show_logs(t: Any, tail: int = 15) -> None:
    for service in ("adapter", bundle.INGRESS_SERVICE):
        result = t.compose(f"logs --no-color --tail {tail} {shlex.quote(service)}", check=False)
        text = (result.stdout + result.stderr).strip()
        if text:
            say(f"--- {service} ---\n{text}")


# ---- servers and connections ------------------------------------------------------------------
def _resolve_preset(args: argparse.Namespace) -> None:
    """Fill in the address and sign-in method from a well-known server, unless given explicitly."""
    preset = presets.find(args.server)
    if not args.endpoint:
        if preset is None:
            raise SystemExit(
                f"{args.server!r} is not a well-known server: give its address with --endpoint.\n"
                "Well-known servers:\n  " + "\n  ".join(presets.describe())
            )
        args.endpoint = preset.endpoint
    if preset is not None and args.endpoint == preset.endpoint:
        args.credential = args.credential or preset.credential
        args.secret_name = args.secret_name or preset.secret_name
        args.display_name = args.display_name or preset.name
        args.token_help = preset.token_help
    args.credential = args.credential or "none"


def cmd_add_server(args: argparse.Namespace) -> None:
    check_server_id(args.server)  # before anything is stored or contacted
    _resolve_preset(args)
    dep, provider = _context(args)
    t = provider.target()
    admin = Admin(t, actor=args.actor, dev_network=args.dev_network)
    server_id, label = args.server, args.label or args.display_name or args.server
    secret_name: str | None = None
    if args.credential == "env_token":
        chosen: str = args.secret_name or f"{args.server.upper().replace('-', '_')}_TOKEN"
        secret_name = chosen
        supplied = os.environ.get(chosen)
        if supplied:  # a token given now replaces one stored earlier, such as a rejected one
            replacing = chosen in bundle.list_secrets(t)
            bundle.set_secret(t, chosen, supplied)
            verb = "replaced" if replacing else "stored"
            say(f"  {verb} {chosen} on the VM (readable only by the adapter)")
        elif chosen in bundle.list_secrets(t):
            say(f"  using the {chosen} already stored on the VM")
        else:
            if getattr(args, "token_help", None) and sys.stdin.isatty():
                say(f"  {args.token_help}")
            value = _secret_value(chosen, f"The token for {server_id} ({chosen})")
            assert value
            bundle.set_secret(t, chosen, value)
            say(f"  stored {chosen} on the VM (readable only by the adapter)")
    profile = {"none": "none", "env_token": "bearer", "oauth": "oauth"}[args.credential]

    known = {str(item.get("serverId")): item for item in admin.servers()}
    if server_id in known:
        if known[server_id].get("endpoint") != args.endpoint:
            raise SystemExit(
                f"{server_id} is already registered at {known[server_id].get('endpoint')}. "
                "Use another --name for the new address, or remove it first."
            )
        connections = [c for c in admin.links() if c.get("serverId") == server_id]
        if connections:
            say(
                f"{server_id} is already connected ({len(connections)} connection(s)): "
                "nothing to do. See them with `muse-vm servers`."
            )
            return
        say(f"{server_id} was registered earlier but never finished: continuing")
    else:
        say(f"registering {server_id} at {args.endpoint}")
        admin.register(server_id, args.display_name or server_id, args.endpoint, profile)
    linked: Linked | None = None
    if args.credential == "oauth":
        linked = admin.link(server_id, label, "oauth", None)
        say()
        say("  Sign in to the server with this link, then press Enter:")
        say(f"    {linked.login_url}")
        if not sys.stdin.isatty():
            raise SystemExit("OAuth sign-in needs you at a terminal: run this command in one.")
        input("  ")
        found = admin.discover(server_id, connection_id=linked.connection_id)
    else:
        found = admin.discover(server_id, token_env=secret_name)
    say(f"  found {found} tools, resources and prompts")

    if args.policy:
        policies = json.loads(Path(args.policy).read_text())
    else:
        policies = admin.draft_policy(server_id, expose_reads=not args.no_reads)
    exposed = pick_exposure(policies, allow=args.allow)
    say("  exposed to Muse:" if exposed else "  nothing is exposed yet (use --allow TOOL).")
    for item in exposed:
        say(f"    {item}")
    admin.publish(server_id, "1.0.0", policies)
    if linked is None:
        linked = admin.link(server_id, label, args.credential, secret_name)
    dep.servers = sorted({*dep.servers, server_id})
    dep.save()
    _show_linked(linked, copy=not args.no_copy)


def _next_version(current: str) -> str:
    parts = current.split(".")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        return f"{parts[0]}.{int(parts[1]) + 1}.0"
    return "1.0.0"


def cmd_expose(args: argparse.Namespace) -> None:
    """Change which of a server's tools Muse can use, as a new reviewable catalog version."""
    _, provider = _context(args)
    server_id = check_server_id(args.server)
    admin = Admin(provider.target(), actor=args.actor)
    live_version, live = admin.published(server_id)
    drafted = admin.draft_policy(server_id, expose_reads=not args.no_reads)
    # What is already published keeps its settings; only the changes asked for are applied.
    policies = [dict(live.get(str(item["primitiveKey"]), item)) for item in drafted]
    known = {str(item["primitiveKey"]).split(":", 1)[1] for item in policies}
    unknown = set(args.deny or []) - known
    if unknown:
        raise SystemExit(f"The server offers no such tool: {', '.join(sorted(unknown))}.")
    for item in policies:
        if str(item["primitiveKey"]).split(":", 1)[1] in (args.deny or []):
            item["allowed"] = False
    exposed = pick_exposure(policies, allow=args.allow)
    version = _next_version(live_version)
    say(f"Publishing {server_id} {version}; Muse will be able to use:")
    for item in exposed:
        say(f"  {item}")
    admin.publish(server_id, version, policies)
    say("Done. Muse sees the change on its next request; its address and token are unchanged.")


def _plan_from(args: argparse.Namespace) -> tuple[config.Plan, Path | None]:
    """The config file (given, or muse-vm.toml here), with command-line flags on top."""
    try:
        plan, path = config.discover(args.config)
    except config.ConfigError as error:
        raise SystemExit(str(error)) from None
    for field_name in ("name", "cloud", "ssh", "project", "zone", "machine_type", "tz"):
        value = getattr(args, field_name, None)
        if value:
            setattr(plan, field_name, value)
    for field_name in ("budget_usd", "idle_stop_hours"):
        value = getattr(args, field_name, None)
        if value is not None:
            setattr(plan, field_name, value)
    if args.ssh_opt:
        plan.ssh_opts = list(args.ssh_opt)
    if args.egress_allow:
        plan.egress_allow = [item for item in args.egress_allow.split(",") if item]
    if args.cloud or args.ssh or args.local:
        # choosing on the command line replaces whatever the file chose
        plan.cloud = args.cloud
        plan.ssh = args.ssh
        plan.local = bool(args.local)
    if args.yes:
        plan.yes = True
    if getattr(args, "replace_leftovers", False):
        plan.replace_leftovers = True
    if args.server:
        plan.servers = [config.ServerPlan(server=item) for item in args.server]
    return plan, path


def cmd_setup(args: argparse.Namespace) -> None:
    if args.no_browser:
        os.environ["MUSE_VM_NO_BROWSER"] = "1"
    plan, path = _plan_from(args)
    name = check_name(plan.name or DEFAULT_NAME)
    terminal = sys.stdin.isatty() and sys.stdout.isatty()
    wizard_io = wizard.Io(
        say=say,
        ask=wizard.ask_at_terminal,
        interactive=terminal and not args.non_interactive,
        verbose=args.verbose,
    )
    wizard.run(
        plan,
        name=name,
        runner=_runner(args),
        run_command=main,
        io=wizard_io,
        save_to=Path.cwd() / config.FILENAME if (args.save or path is None) else None,
        config_existed=path is not None and not args.save,
    )


def cmd_doctor(args: argparse.Namespace) -> None:
    report = Report()
    runner = _runner(args)
    doctor.local_checks(report)
    if args.local:
        doctor.local_machine_checks(report, runner)
    elif args.ssh:
        doctor.ssh_checks(report, runner, args.ssh, args.ssh_opt or [])
    elif args.cloud == "gcp" or (not args.ssh and doctor.gcloud_facts(runner)):
        doctor.gcp_checks(report, runner, args.project, args.zone)
    for line in report.lines():
        say(line)
    if not report.ok:
        raise SystemExit("Fix the items marked ✗ and run `muse-vm doctor` again.")
    say("Ready.")


def cmd_presets(args: argparse.Namespace) -> None:
    say("Well-known servers: `muse-vm add-server <name>` needs nothing else.")
    for line in presets.describe():
        say(f"  {line}")


def cmd_servers(args: argparse.Namespace) -> None:
    _, provider = _context(args)
    admin = Admin(provider.target(), actor=args.actor)
    servers = admin.servers()
    links = admin.links()
    if not servers:
        say("No MCP servers are connected yet. Add one with `muse-vm add-server`.")
        return
    for server in servers:
        mine = [link for link in links if link.get("serverId") == server.get("serverId")]
        active = "enabled" if server.get("enabled") else "disabled"
        say(
            f"{server.get('serverId')}  {server.get('endpoint')}  "
            f"({active}, {len(mine)} connection(s))"
        )
        for link in mine:
            state = "revoked" if link.get("revoked") or link.get("revokedAt") else "active"
            say(
                f"    {link.get('connectionId')}  {link.get('label')}  "
                f"{link.get('credential', '')}  {state}"
            )


def _connection(admin: Admin, server_id: str) -> dict[str, Any]:
    mine = [
        link
        for link in admin.links()
        if link.get("serverId") == server_id and not (link.get("revoked") or link.get("revokedAt"))
    ]
    if not mine:
        raise SystemExit(f"There is no active connection for {server_id!r}. See `muse-vm servers`.")
    return mine[0]


def cmd_rotate_token(args: argparse.Namespace) -> None:
    _, provider = _context(args)
    admin = Admin(provider.target(), actor=args.actor)
    link = _connection(admin, args.server)
    result = admin.rotate_token(str(link["connectionId"]))
    say(f"New token for {args.server}: {result.get('token')}")
    say(f"New connection code: {result.get('connectionCode')}")
    say(
        "The old token and every Muse session for this connection stopped working. "
        "Give Muse the new token."
    )


def cmd_unlink(args: argparse.Namespace) -> None:
    _, provider = _context(args)
    admin = Admin(provider.target(), actor=args.actor)
    link = _connection(admin, args.server)
    admin.unlink(str(link["connectionId"]))
    say(f"Muse can no longer use {args.server}.")


def cmd_secret(args: argparse.Namespace) -> None:
    _, provider = _context(args)
    t = provider.target()
    if args.action == "list":
        for name in bundle.list_secrets(t):
            say(name)
        return
    if not args.secret_name:
        raise SystemExit("Say which secret: muse-vm secret set NAME")
    value = _secret_value(args.secret_name, f"The value for {args.secret_name}")
    assert value
    bundle.set_secret(t, args.secret_name, value)
    say(f"Stored {args.secret_name}. The adapter reads it the next time it is needed; no restart.")


# ---- day to day -------------------------------------------------------------------------------
def cmd_list(args: argparse.Namespace) -> None:
    found = Deployment.all()
    if not found:
        say("No deployments.")
    for dep in found:
        say(f"{dep.name}  {dep.provider}  {dep.base_url or '-'}  {dep.status}")


def cmd_status(args: argparse.Namespace) -> None:
    dep, provider = _context(args, ready=False)
    say(f"{dep.name}: {dep.provider}, {dep.base_url or 'no address yet'} ({dep.status})")
    for key, value in provider.status().items():
        say(f"  {key}: {value}")
    try:
        provider.ensure_ready()
    except SystemExit as stopped:
        say(f"  ! {stopped}")
        return
    t = provider.target()
    containers = t.compose("ps --format '{{.Service}} {{.State}} {{.Health}}'", check=False)
    for line in containers.stdout.strip().splitlines():
        say(f"  container {line}")
    if dep.base_url:
        healthy, why = selfcheck.wait_healthy(dep.base_url, timeout=15, say=lambda _: None)
        say(f"  public /health: {'ok' if healthy else 'not answering (' + why + ')'}")
    try:
        servers = Admin(t, actor=args.actor).servers()
        say(f"  servers: {', '.join(str(s.get('serverId')) for s in servers) or 'none'}")
    except CommandError:
        say("  servers: could not be read (is the adapter running?)")


def cmd_logs(args: argparse.Namespace) -> None:
    _, provider = _context(args)
    result = provider.target().compose(
        f"logs --no-color --tail {int(args.tail)} {shlex.quote(args.service)}", check=False
    )
    say((result.stdout + result.stderr).rstrip())


def cmd_selfcheck(args: argparse.Namespace) -> None:
    dep, provider = _context(args)
    report = selfcheck.run(provider.target(), str(dep.base_url), wait=args.wait, say=say)
    for line in report.lines():
        say(line)
    if not report.ok:
        raise SystemExit(1)
    if report.address_pending:
        say(PENDING_NOTE.format(name=dep.name))
    elif dep.extra.pop("address_pending", None):
        dep.save()
        say("The public address now answers.")


def cmd_start(args: argparse.Namespace) -> None:
    dep, provider = _context(args, ready=False)
    provider.start()
    healthy, why = selfcheck.wait_healthy(str(dep.base_url), timeout=300, say=say)
    say("Running." if healthy else f"Started, but not answering yet: {why}")


def cmd_stop(args: argparse.Namespace) -> None:
    _, provider = _context(args)
    provider.stop()
    say("Stopped.")


def cmd_upgrade(args: argparse.Namespace) -> None:
    dep, provider = _context(args)
    t = provider.target()
    previous = bundle.set_version(t)
    if dep.build:
        source = bundle.find_build_source()
        if source is None:
            raise SystemExit(
                "Nothing to rebuild from: upgrade muse-mcp-adapter first (`pipx upgrade "
                "muse-mcp-adapter`, or `git pull` in your checkout), then run `muse-vm upgrade`."
            )
        _refuse_unreviewed(source, args.allow_dirty)
        revision = bundle.upload_source(t, source)
        say(f"  building from commit {revision}")
        t.compose("build", stream=True, timeout=3600)
    else:
        t.compose("pull --quiet", stream=True, timeout=1800)
    env = bundle.parse_env(t.get(".env"))
    bundle.install(t, env, t.get("config/serve.json") or bundle.serve_config(str(dep.hostname)))
    bundle.install_units(t, idle_hours=dep.extra.get("idle_stop_hours"))
    t.compose("up -d --remove-orphans", stream=True)
    if bundle.forget_auth_key(t):  # deployments made before this was cleaned up after joining
        t.compose("up -d --no-deps tailscale", stream=True)
    dep.save()
    report = selfcheck.run(t, str(dep.base_url), wait=300, say=say)
    for line in report.lines():
        say(line)
    moved = f"Upgraded from {previous} to {__version__}" if previous else f"Rebuilt {__version__}"
    say(f"{moved}. The address, the connections and the data are unchanged.")


def cmd_backup(args: argparse.Namespace) -> None:
    dep, provider = _context(args)
    t = provider.target()
    passphrase = _secret_value("MUSE_VM_BACKUP_PASSPHRASE", "A passphrase to encrypt the backup")
    assert passphrase
    t.sh("sh ./backup.sh", timeout=600)
    name = t.sh("ls -1 backups | grep '^adapter-' | sort | tail -1").stdout.strip()
    if not name:
        raise SystemExit("The adapter made no backup. Look at `muse-vm logs`.")
    say(f"  fetching {name}…")
    archive = t.get_bytes(f"tar -C backups -czf - {shlex.quote(name)}")
    out = Path(args.out or f"{dep.name}-{name}.mmavm").expanduser()
    descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(crypt.encrypt(archive, passphrase))
    say(
        f"Saved {out} ({out.stat().st_size} bytes, encrypted). It holds the adapter's key and "
        "tokens; "
        "keep it somewhere safe."
    )


def cmd_restore(args: argparse.Namespace) -> None:
    _, provider = _context(args)
    t = provider.target()
    passphrase = _secret_value("MUSE_VM_BACKUP_PASSPHRASE", "The passphrase of the backup")
    assert passphrase
    archive = crypt.decrypt(Path(args.source).expanduser().read_bytes(), passphrase)
    _confirm("Restore over the adapter's current data (it is moved aside, not deleted)?", args.yes)
    t.put_bytes("tar -C backups -xzf -", archive)
    t.sh("chown -R 10001:10001 backups")
    name = t.sh("ls -1 backups | grep '^adapter-' | sort | tail -1").stdout.strip()
    t.compose("stop adapter")
    flags = "--database /data/adapter.db --config /config/serve.json"
    t.compose(
        f"run --rm --no-deps -T adapter {flags} restore --from /backups/{shlex.quote(name)} --force"
    )
    t.compose("up -d adapter")
    say(
        f"Restored {name}. Secrets for servers that need a token are not part of a backup: "
        "set them again with `muse-vm secret set NAME`."
    )


def cmd_down(args: argparse.Namespace) -> None:
    dep = _pick(args.name)
    if args.forget:
        _confirm(
            f"Forget deployment {dep.name!r}? Nothing in the cloud or on the machine is touched.",
            args.yes,
        )
        dep.delete()
        say(f"Forgot {dep.name!r}: only the local record was removed.")
        return
    provider = provider_for(dep, _runner(args), say, {"yes": args.yes})
    _confirm(f"Remove deployment {dep.name!r} and everything it created?", args.yes)
    provider.teardown(keep_data=args.keep_data)
    dep.delete()
    say("Removed.")


# ---- parser -----------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="muse-vm", description=__doc__)
    parser.add_argument("--version", action="version", version=f"muse-vm {__version__}")
    commands = parser.add_subparsers(dest="command")

    def common(
        sub: argparse.ArgumentParser, default_name: str | None = None
    ) -> argparse.ArgumentParser:
        sub.add_argument("--name", default=default_name, help="deployment name")
        sub.add_argument("--verbose", "-v", action="store_true", help="print every command it runs")
        sub.add_argument(
            "--actor",
            default=os.environ.get("USER", "muse-vm"),
            help="name recorded in the adapter's administrative audit",
        )
        return sub

    up = common(commands.add_parser("up", help="create a deployment"), DEFAULT_NAME)
    where = up.add_mutually_exclusive_group()
    where.add_argument("--ssh", metavar="USER@HOST", help="use an existing Linux machine over SSH")
    where.add_argument(
        "--local", action="store_true", help="install on this machine (Linux, root or sudo)"
    )
    where.add_argument(
        "--cloud", choices=sorted(set(PROVIDERS) - {"ssh", "local"}), help="create a cloud VM"
    )
    up.add_argument("--tz", default=os.environ.get("MMA_TZ", "UTC"))
    up.add_argument(
        "--egress-allow",
        metavar="CIDRS",
        help="private ranges the adapter may reach (default: none)",
    )
    up.add_argument(
        "--image",
        help="use this adapter image from your own registry instead of building from the "
        "checkout (needs --guard-image)",
    )
    up.add_argument("--guard-image", help="egress guard image from your own registry")
    up.add_argument(
        "--ssh-opt", action="append", metavar="OPT", help="extra ssh option, e.g. --ssh-opt=-p2222"
    )
    up.add_argument("--project", help="GCP project id")
    up.add_argument("--zone", help="GCP zone (default: gcloud config, else us-central1-a)")
    up.add_argument("--machine-type", help="GCE machine type (default e2-small)")
    up.add_argument(
        "--budget-usd", type=float, help="create a monthly budget alert for the project"
    )
    up.add_argument(
        "--idle-stop-hours", type=int, help="power the VM off after N hours without activity"
    )
    up.add_argument(
        "--no-snapshots", action="store_true", help="do not schedule daily disk snapshots"
    )
    up.add_argument(
        "--wait", type=float, default=None, help="seconds to wait for the public address"
    )
    up.add_argument("--no-browser", action="store_true", help="print links instead of opening them")
    up.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    up.add_argument(
        "--force", action="store_true", help="reuse an existing deployment directory on the host"
    )
    up.add_argument(
        "--replace-leftovers",
        action="store_true",
        help="remove what an earlier run of this name left in the cloud project, then start fresh",
    )
    up.add_argument(
        "--keep-on-failure", action="store_true", help="do not roll back a failed deployment"
    )
    up.add_argument(
        "--allow-dirty",
        action="store_true",
        help="build even if the checkout has uncommitted changes (not recommended)",
    )
    up.set_defaults(func=cmd_up)

    sp = common(commands.add_parser("add-server", help="connect an MCP server and link it to Muse"))
    sp.add_argument(
        "server",
        help="a well-known server (" + ", ".join(presets.PRESETS) + ") or any short id of your own",
    )
    sp.add_argument(
        "--endpoint",
        help="the server's public HTTPS MCP address (not needed for a well-known server)",
    )
    sp.add_argument("--display-name")
    sp.add_argument("--label", help="the connection's label (default: the id)")
    sp.add_argument(
        "--credential",
        choices=("none", "env_token", "oauth"),
        help="how the adapter signs in to the server (default: the well-known server's, else none)",
    )
    sp.add_argument("--secret-name", help="env_token: the secret's name (default: <ID>_TOKEN)")
    sp.add_argument("--policy", help="a reviewed policy file; otherwise one is drafted")
    sp.add_argument("--no-reads", action="store_true", help="do not pre-approve read-only tools")
    sp.add_argument(
        "--allow", action="append", metavar="TOOL", help="also expose this tool (repeatable)"
    )
    sp.add_argument("--no-copy", action="store_true")
    sp.add_argument("--dev-network", action="store_true", help=argparse.SUPPRESS)
    sp.add_argument("--yes", "-y", action="store_true")
    sp.set_defaults(func=cmd_add_server)

    sp = common(
        commands.add_parser(
            "setup", help="guided setup: the adapter, then the servers you want, in one run"
        ),
        None,
    )
    sp.add_argument(
        "--config", "-f", help=f"a settings file (default: ./{config.FILENAME} if present)"
    )
    sp.add_argument("--cloud", choices=("gcp",), help="create a Google Cloud VM")
    sp.add_argument("--ssh", metavar="USER@HOST", help="use an existing Linux machine over SSH")
    sp.add_argument("--local", action="store_true", help="install on this machine (Linux)")
    sp.add_argument("--ssh-opt", action="append", metavar="OPT")
    sp.add_argument("--project", help="GCP project (default: gcloud's)")
    sp.add_argument("--zone")
    sp.add_argument("--machine-type")
    sp.add_argument("--budget-usd", type=float)
    sp.add_argument("--idle-stop-hours", type=int)
    sp.add_argument("--egress-allow", metavar="CIDRS")
    sp.add_argument("--tz")
    sp.add_argument(
        "--server",
        action="append",
        metavar="NAME",
        help="a well-known server to connect (repeatable)",
    )
    sp.add_argument(
        "--yes", "-y", action="store_true", help="accept that this creates billable resources"
    )
    sp.add_argument("--save", action="store_true", help=f"write the choices to ./{config.FILENAME}")
    sp.add_argument("--non-interactive", action="store_true", help="never ask; fail instead")
    sp.add_argument("--no-browser", action="store_true", help="print links instead of opening them")
    sp.set_defaults(func=cmd_setup)
    sp = common(commands.add_parser("doctor", help="check what would stop a deployment"))
    sp.add_argument("--ssh", metavar="USER@HOST", help="check this machine too")
    sp.add_argument("--local", action="store_true", help="check this machine as the target")
    sp.add_argument("--cloud", choices=("gcp",), help="check Google Cloud access")
    sp.add_argument("--project")
    sp.add_argument("--zone")
    sp.add_argument("--ssh-opt", action="append", metavar="OPT")
    sp.set_defaults(func=cmd_doctor)
    common(commands.add_parser("presets", help="list the well-known servers")).set_defaults(
        func=cmd_presets
    )
    common(
        commands.add_parser("servers", help="the connected servers and their connections")
    ).set_defaults(func=cmd_servers)
    for command, handler, text in (
        ("rotate-token", cmd_rotate_token, "issue a new token for a server's connection"),
        ("unlink", cmd_unlink, "stop Muse using a server"),
    ):
        sp = common(commands.add_parser(command, help=text))
        sp.add_argument("server")
        sp.set_defaults(func=handler)

    sp = common(commands.add_parser("expose", help="change which tools of a server Muse can use"))
    sp.add_argument("server")
    sp.add_argument("--allow", action="append", metavar="TOOL", help="also expose this tool")
    sp.add_argument("--deny", action="append", metavar="TOOL", help="stop exposing this tool")
    sp.add_argument("--no-reads", action="store_true", help="do not pre-approve read-only tools")
    sp.set_defaults(func=cmd_expose)
    sp = common(commands.add_parser("secret", help="store or list the secrets servers need"))
    sp.add_argument("action", choices=("set", "list"))
    sp.add_argument("secret_name", nargs="?")
    sp.set_defaults(func=cmd_secret)

    common(commands.add_parser("list", help="list deployments")).set_defaults(func=cmd_list)
    common(commands.add_parser("status", help="health, containers and servers")).set_defaults(
        func=cmd_status
    )
    sp = common(commands.add_parser("logs", help="container logs"))
    sp.add_argument("service", nargs="?", default="adapter")
    sp.add_argument("--tail", default=100)
    sp.set_defaults(func=cmd_logs)
    sp = common(commands.add_parser("selfcheck", help="run the deployment checks again"))
    sp.add_argument("--wait", type=float, default=30)
    sp.set_defaults(func=cmd_selfcheck)
    common(
        commands.add_parser("start", help="start the adapter (cloud: start the VM)")
    ).set_defaults(func=cmd_start)
    common(commands.add_parser("stop", help="stop the adapter (cloud: stop the VM)")).set_defaults(
        func=cmd_stop
    )
    sp = common(
        commands.add_parser(
            "upgrade", help="move to this version; the address, connections and data stay"
        )
    )
    sp.add_argument("--allow-dirty", action="store_true", help="build with uncommitted changes")
    sp.set_defaults(func=cmd_upgrade)
    sp = common(
        commands.add_parser("backup", help="download an encrypted backup of the adapter's state")
    )
    sp.add_argument("--out", help="where to save it (default: ./<name>-adapter-<time>.mmavm)")
    sp.set_defaults(func=cmd_backup)
    sp = common(
        commands.add_parser("restore", help="restore an encrypted backup onto this deployment")
    )
    sp.add_argument("source", help="a .mmavm file")
    sp.add_argument("--yes", "-y", action="store_true")
    sp.set_defaults(func=cmd_restore)
    sp = common(commands.add_parser("down", help="remove the deployment and everything it created"))
    sp.add_argument("--keep-data", action="store_true", help="keep the data (cloud: the boot disk)")
    sp.add_argument(
        "--forget",
        action="store_true",
        help="only delete the local record (the cloud resources are already gone)",
    )
    sp.add_argument("--yes", "-y", action="store_true")
    sp.set_defaults(func=cmd_down)
    return parser


WELCOME = """muse-vm puts the Muse MCP Adapter on a machine you control and looks after it.

  muse-vm setup       guided: checks, creates the adapter, connects the servers you pick
  muse-vm doctor      check what would stop a deployment, before anything is created
  muse-vm presets     the well-known servers (GitHub, Linear, Microsoft Learn, ...)
  muse-vm --help      every command

Start with `muse-vm setup`."""


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not hasattr(args, "func"):
        say(WELCOME)
        return 0
    try:
        args.func(args)
    except CommandError as error:
        print(f"\n✗ {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except SystemExit as stopped:
        if stopped.code not in (None, 0):
            if isinstance(stopped.code, str):
                print(stopped.code, file=sys.stderr)
            return 1 if isinstance(stopped.code, str) else int(stopped.code or 1)
    return 0
