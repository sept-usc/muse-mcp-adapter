# pyright: basic, reportArgumentType=false
"""The simple-onboarding pieces: presets, the settings file, the doctor, and `muse-vm setup`."""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from muse_vm import browser, cli, config, doctor, ingress, presets
from muse_vm import setup as wizard
from muse_vm.runner import Result
from muse_vm.selfcheck import Report
from muse_vm.state import Deployment
from tests.fixtures.vm_fakes import FakeRunner, FakeTarget


# ---- well-known servers ----------------------------------------------------------------------
def _add_server_args(*argv: str) -> Any:
    return cli.build_parser().parse_args(["add-server", *argv])


def test_a_well_known_server_needs_nothing_else() -> None:
    args = _add_server_args("github")
    cli._resolve_preset(args)  # type: ignore[reportPrivateUsage]
    assert args.endpoint == "https://api.githubcopilot.com/mcp/"
    assert (args.credential, args.secret_name, args.display_name) == (
        "env_token",
        "GITHUB_TOKEN",
        "GitHub",
    )
    assert "personal-access-tokens" in args.token_help


def test_an_unknown_server_without_an_address_lists_the_well_known_ones() -> None:
    args = _add_server_args("mine")
    with pytest.raises(SystemExit) as stopped:
        cli._resolve_preset(args)  # type: ignore[reportPrivateUsage]
    for name in presets.PRESETS:
        assert name in str(stopped.value)
    assert "--endpoint" in str(stopped.value)


def test_explicit_flags_beat_the_preset_and_a_custom_server_still_works() -> None:
    args = _add_server_args("github", "--credential", "none", "--secret-name", "OTHER")
    cli._resolve_preset(args)  # type: ignore[reportPrivateUsage]
    assert (args.credential, args.secret_name) == ("none", "OTHER")

    custom = _add_server_args("tracker", "--endpoint", "https://tracker.example.com/mcp")
    cli._resolve_preset(custom)  # type: ignore[reportPrivateUsage]
    assert (custom.credential, custom.endpoint) == ("none", "https://tracker.example.com/mcp")

    # a different address for a well-known id means it is not that server: no preset settings
    moved = _add_server_args("github", "--endpoint", "https://ghe.example.com/mcp")
    cli._resolve_preset(moved)  # type: ignore[reportPrivateUsage]
    assert moved.credential == "none" and moved.secret_name is None


def test_every_preset_is_usable() -> None:
    for preset in presets.PRESETS.values():
        assert preset.endpoint.startswith("https://") and preset.credential in (
            "none",
            "env_token",
            "oauth",
        )
        assert (preset.credential == "env_token") == bool(preset.secret_name)
    assert presets.find("GitHub") is presets.PRESETS["github"]


# ---- the settings file -----------------------------------------------------------------------
def test_a_settings_file_round_trips_and_names_what_it_holds() -> None:
    text = """
name = "prod"
cloud = "gcp"
project = "my-project"
budget_usd = 20
idle_stop_hours = 8
egress_allow = ["10.0.0.0/24"]
yes = true

[[servers]]
preset = "github"
allow = ["issue_write"]
token_file = "~/gh-token"

[[servers]]
id = "tracker"
endpoint = "https://tracker.example.com/mcp"
credential = "env_token"
secret_name = "TRACKER_TOKEN"
"""
    plan = config.parse(text)
    assert (plan.name, plan.cloud, plan.budget_usd, plan.yes) == ("prod", "gcp", 20, True)
    assert plan.servers[0].server == "github" and plan.servers[0].allow == ["issue_write"]
    assert plan.servers[1].endpoint == "https://tracker.example.com/mcp"
    again = config.parse(config.dump(plan))
    assert again == plan


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("cloud = 'gcp'\nprojct = 'x'", "unknown key 'projct'"),
        ("budget_usd = 'lots'", "wrong type"),
        ("yes = 1", "wrong type"),
        ("cloud = 'aws'", 'cloud must be "gcp"'),
        ("cloud = 'gcp'\nssh = 'a@b'", "choose one of cloud, ssh or local"),
        ("local = true\nssh = 'a@b'", "choose one of cloud, ssh or local"),
        ("[[servers]]\nallow = []", "needs a preset or an id"),
        ("[[servers]]\npreset = 'x'\ncredential = 'magic'", "credential must be"),
        ("name = ", "not valid TOML"),
    ],
)
def test_a_bad_settings_file_is_refused_with_a_reason(text: str, message: str) -> None:
    with pytest.raises(config.ConfigError, match=message):
        config.parse(text)


def test_the_settings_file_is_found_by_flag_or_in_the_current_folder(tmp_path: Path) -> None:
    assert config.discover(None, tmp_path) == (config.Plan(), None)
    (tmp_path / config.FILENAME).write_text("name = 'here'\n")
    plan, path = config.discover(None, tmp_path)
    assert plan.name == "here" and path == tmp_path / config.FILENAME
    with pytest.raises(config.ConfigError, match="no such file"):
        config.discover(str(tmp_path / "missing.toml"))


def test_a_saved_plan_holds_no_secret_only_where_one_is_kept() -> None:
    plan = config.Plan(
        cloud="gcp",
        servers=[config.ServerPlan("github", token_file="~/gh-token")],
    )
    text = config.dump(plan)
    assert "token_file" in text and "ghp_" not in text and "tskey" not in text


# ---- the doctor ------------------------------------------------------------------------------
def _gcloud(**answers: Any) -> FakeRunner:
    runner = FakeRunner()
    runner.when("services list", stdout=answers.get("apis", "compute.googleapis.com\n"))
    if answers.get("expired"):
        runner.when("print-access-token", returncode=1, stderr="Reauthentication failed")
    runner.when("print-access-token", stdout="token")
    runner.when("config get-value project", stdout=answers.get("project", "my-proj"))
    runner.when("config get-value compute/zone", stdout="(unset)")
    runner.when("auth list", stdout=answers.get("account", "me@example.com"))
    runner.when("gcloud --version", stdout="Google Cloud SDK")
    return runner


def _names(report: Report) -> dict[str, bool]:
    return {name: ok for name, ok, _, _ in report.checks}


def test_the_doctor_reads_defaults_from_gcloud() -> None:
    facts = doctor.gcloud_facts(_gcloud())
    assert facts is not None
    assert (facts.project, facts.account, facts.zone, facts.credentials_ok) == (
        "my-proj",
        "me@example.com",
        None,  # "(unset)" means not set
        True,
    )


def test_expired_gcloud_credentials_are_caught_even_though_the_account_is_listed() -> None:
    report = Report()
    doctor.gcp_checks(report, _gcloud(expired=True), None, None)
    assert report.ok is False
    names = _names(report)
    assert names["gcloud is signed in"] is True and names["gcloud credentials are valid"] is False
    assert not any("googleapis" in n for n in names)  # it stops before asking the API


def test_a_missing_gcloud_or_project_says_how_to_fix_it() -> None:
    report = Report()
    doctor.gcp_checks(report, FakeRunner([("gcloud --version", Result(1, "", "nope"))]), None, None)
    assert "gcloud" in report.lines()[0] and "cloud.google.com/sdk" in report.lines()[0]

    report = Report()
    doctor.gcp_checks(report, _gcloud(project="(unset)"), None, None)
    assert "gcloud config set project" in " ".join(report.lines())


def test_a_disabled_api_is_a_note_not_a_blocker() -> None:
    report = Report()
    doctor.gcp_checks(report, _gcloud(apis=""), None, "us-west1-b")
    assert report.ok is True  # `up --yes` enables the APIs
    assert any("compute.googleapis.com" in line and "!" in line for line in report.lines())


def test_the_doctor_checks_an_ssh_target() -> None:
    report = Report()
    runner = FakeRunner()
    runner.when("uname -s -m", stdout="Linux aarch64\n0\n")
    runner.when("registry-1.docker.io", stdout="docker=22\npypi=0\n")
    doctor.ssh_checks(report, runner, "me@host", [])
    assert report.ok and all(ok for _, ok, _, _ in report.checks)

    bad = Report()
    refused = FakeRunner([("uname -s -m", Result(255, "", "Permission denied (publickey)"))])
    doctor.ssh_checks(bad, refused, "me@host", [])
    assert bad.ok is False and "Permission denied" in bad.lines()[0]

    mac = Report()
    odd = FakeRunner([("uname -s -m", Result(0, "Darwin arm64\n501\n", ""))])
    doctor.ssh_checks(mac, odd, "me@host", [])
    assert mac.ok is False


# ---- the browser -----------------------------------------------------------------------------
def test_links_open_only_for_a_person_at_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(browser.webbrowser, "open", lambda url, new=0: opened.append(url) or True)
    monkeypatch.setattr(browser, "interactive", lambda: False)
    assert browser.open_link("https://login.tailscale.com/a/x") is False and not opened
    monkeypatch.setattr(browser, "interactive", lambda: True)
    assert browser.open_link("https://login.tailscale.com/a/x") is True
    assert browser.open_link("http://insecure.example.com") is False  # https links only
    assert opened == ["https://login.tailscale.com/a/x"]


def test_the_tailscale_links_are_opened_for_the_person(monkeypatch: pytest.MonkeyPatch) -> None:
    import json as _json

    class Tailnet(FakeTarget):
        states = [
            _json.dumps({"BackendState": "NeedsLogin"}),
            _json.dumps(
                {"BackendState": "Running", "Self": {"DNSName": "x.t.ts.net.", "CapMap": {}}}
            ),
            _json.dumps(
                {
                    "BackendState": "Running",
                    "Self": {"DNSName": "x.t.ts.net.", "CapMap": {"https": [], "funnel": []}},
                }
            ),
        ]

        def sh(self, script: str, *args: Any, **kwargs: Any) -> Result:  # type: ignore[override]
            super().sh(script, *args, **kwargs)
            if "tailscale status --json" in script:
                return Result(0, self.states.pop(0) if len(self.states) > 1 else self.states[0], "")
            if "query-feature" in script:
                return Result(
                    0, _json.dumps({"URL": "https://login.tailscale.com/f/funnel?q=1"}), ""
                )
            if "serve status" in script:
                return Result(0, _json.dumps({"AllowFunnel": {"x.t.ts.net:443": True}}), "")
            if "logs" in script:
                return Result(0, "visit https://login.tailscale.com/a/abc123", "")
            return Result(0, "", "")

    opened: list[str] = []
    name = ingress.hostname_after_start(
        Tailnet(),
        say=lambda _line: None,
        sleep=lambda _s: None,
        open_link=lambda url: opened.append(url) or True,
    )
    assert name == "x.t.ts.net"
    assert opened == [
        "https://login.tailscale.com/a/abc123",
        "https://login.tailscale.com/f/funnel?q=1",
    ]


# ---- `muse-vm setup` -------------------------------------------------------------------------
class _Terminal:
    def __init__(self, *answers: str, interactive: bool = True) -> None:
        self.answers = list(answers)
        self.said: list[str] = []
        self.asked: list[str] = []
        self.io = wizard.Io(self.said.append, self._ask, interactive)

    def _ask(self, question: str) -> str:
        self.asked.append(question)
        return self.answers.pop(0) if self.answers else ""

    @property
    def text(self) -> str:
        return "\n".join(self.said)


class _Commands:
    def __init__(self, code: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.code = code

    def __call__(self, argv: list[str]) -> int:
        self.calls.append(argv)
        return self.code


@pytest.fixture(autouse=True)
def _wizard_environment(monkeypatch: pytest.MonkeyPatch) -> Any:
    # The menu offers "this machine" only on Linux: tests must not depend on where they run.
    monkeypatch.setattr(wizard.sys, "platform", "darwin")

    def passing(report: Report) -> None:
        report.add("this computer is ready", True)

    monkeypatch.setattr(doctor, "local_checks", passing)
    monkeypatch.delenv("TS_AUTHKEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    yield
    # The wizard puts secrets it reads from files into the environment, for the commands it runs.
    for name in ("TS_AUTHKEY", "GITHUB_TOKEN"):
        os.environ.pop(name, None)


def _run(
    plan: config.Plan, term: _Terminal, commands: _Commands, runner: Any = None, **kw: Any
) -> None:
    wizard.run(
        plan,
        name=kw.pop("name", "muse"),
        runner=runner or _gcloud(),
        run_command=commands,
        io=term.io,
        **kw,
    )


def test_a_person_with_gcloud_is_asked_one_question_and_gets_a_vm_and_servers() -> None:
    term, commands = _Terminal("1", "y", "y", "1,2"), _Commands()  # where, settings, cost, servers
    _run(config.Plan(), term, commands)
    up, learn, github = commands.calls
    assert (
        up[:2] == ["up", "--name"]
        and "--cloud" in up
        and up[up.index("--project") + 1] == "my-proj"
    )
    assert "--yes" in up
    assert learn[:2] == ["add-server", "learn"] and github[:2] == ["add-server", "github"]
    assert "about $13/month" in " ".join(term.said + term.asked)
    assert "browser" in term.text  # it says what the Tailscale links are going to be


def _up_of(commands: _Commands) -> list[str]:
    return commands.calls[0]


def _unfinished(status: str = "creating") -> None:
    from muse_vm.state import Deployment

    Deployment(
        name="muse", provider="gcp", ingress="tailscale-funnel", status=status, project="p"
    ).save()


def test_an_unfinished_earlier_run_is_offered_for_removal_before_a_new_one() -> None:
    _unfinished()
    term, commands = _Terminal("y", "y"), _Commands()  # remove it; cost
    _run(config.Plan(cloud="gcp", project="p", zone="us-central1-a"), term, commands)
    assert "An earlier run named 'muse' did not finish" in term.text
    assert commands.calls[0] == ["down", "--name", "muse", "--yes"]  # cleaned up first
    assert commands.calls[1][0] == "up"


def test_declining_to_remove_an_unfinished_run_changes_nothing() -> None:
    _unfinished()
    commands = _Commands()
    with pytest.raises(SystemExit, match="Nothing was changed"):
        _run(
            config.Plan(cloud="gcp", project="p", zone="us-central1-a"),
            _Terminal("y", "n"),
            commands,
        )
    assert commands.calls == []


def test_a_script_is_told_how_to_clear_an_unfinished_run() -> None:
    _unfinished("failed")
    with pytest.raises(SystemExit, match="down --name muse --forget"):
        _run(config.Plan(cloud="gcp", yes=True), _Terminal(interactive=False), _Commands())


def test_if_the_old_run_cannot_be_removed_the_person_is_told_about_forget() -> None:
    _unfinished()

    class Fails(_Commands):
        def __call__(self, argv: list[str]) -> int:
            super().__call__(argv)
            return 1 if argv[0] == "down" else 0

    with pytest.raises(SystemExit, match="--forget"):
        _run(config.Plan(cloud="gcp", project="p", zone="us-central1-a"), _Terminal("y"), Fails())


def _runner_with_cloud_leftovers() -> FakeRunner:
    runner = _gcloud()
    runner.rules.insert(0, ("networks describe mma-muse", Result(0, "mma-deployment=muse", "")))
    return runner


def test_cloud_leftovers_of_an_earlier_run_are_offered_for_removal() -> None:
    term, commands = _Terminal("y", "y"), _Commands()  # the cost, then remove the leftovers
    plan = config.Plan(cloud="gcp", project="p", zone="us-central1-a")
    _run(plan, term, commands, runner=_runner_with_cloud_leftovers())
    assert "left a network (mma-muse) in project p" in term.text
    assert "--replace-leftovers" in _up_of(commands)


def test_declining_to_remove_cloud_leftovers_changes_nothing() -> None:
    commands = _Commands()
    plan = config.Plan(cloud="gcp", project="p", zone="us-central1-a")
    with pytest.raises(SystemExit, match="Nothing was changed"):
        _run(plan, _Terminal("y", "n"), commands, runner=_runner_with_cloud_leftovers())
    assert commands.calls == []


def test_a_script_is_told_about_the_flag_for_cloud_leftovers() -> None:
    plan = config.Plan(cloud="gcp", yes=True, project="p", zone="us-central1-a")
    with pytest.raises(SystemExit, match="--replace-leftovers"):
        _run(plan, _Terminal(interactive=False), _Commands(), runner=_runner_with_cloud_leftovers())


def test_no_leftovers_means_no_question() -> None:
    term, commands = _Terminal("y"), _Commands()
    _run(config.Plan(cloud="gcp", project="p", zone="us-central1-a"), term, commands)
    assert "left a network" not in term.text and "--replace-leftovers" not in _up_of(commands)


def test_the_google_cloud_settings_are_shown_and_the_defaults_can_be_kept() -> None:
    term, commands = _Terminal("1", "", "y"), _Commands()  # where; Enter keeps the settings; cost
    _run(config.Plan(), term, commands)
    assert "Google Cloud settings:" in term.text and "my-proj" in term.text
    assert "a default: gcloud has no zone set" in term.text  # it says where a value is a guess
    up = _up_of(commands)
    assert up[up.index("--zone") + 1] == "us-central1-a"
    assert up[up.index("--machine-type") + 1] == "e2-small"


def test_the_person_can_choose_another_project_zone_and_machine() -> None:
    term = _Terminal("1", "n", "other-proj-123", "europe-west4-b", "e2-medium", "y")
    commands = _Commands()
    _run(config.Plan(), term, commands)
    up = _up_of(commands)
    assert up[up.index("--project") + 1] == "other-proj-123"
    assert up[up.index("--zone") + 1] == "europe-west4-b"
    assert up[up.index("--machine-type") + 1] == "e2-medium"
    assert "about $25/month" in " ".join(term.said)  # the cost shown is for the chosen machine


def test_a_malformed_zone_is_asked_again() -> None:
    term = _Terminal("1", "n", "", "not-a-zone", "us-west1-b", "", "y")
    commands = _Commands()
    _run(config.Plan(), term, commands)
    assert "That does not look right" in term.text
    assert _up_of(commands)[_up_of(commands).index("--zone") + 1] == "us-west1-b"


def test_settings_that_name_the_project_and_zone_are_not_asked_about_again() -> None:
    term, commands = _Terminal("y"), _Commands()  # only the cost
    _run(config.Plan(cloud="gcp", project="p", zone="asia-east1-a"), term, commands)
    assert "Google Cloud settings:" not in term.text
    assert _up_of(commands)[_up_of(commands).index("--zone") + 1] == "asia-east1-a"


def test_a_script_never_gets_the_settings_question() -> None:
    commands = _Commands()
    _run(config.Plan(cloud="gcp", yes=True), _Terminal(interactive=False), commands)
    assert len(commands.calls) == 1  # and nothing waited for an answer


def test_the_checklist_is_one_line_when_everything_passes_and_complete_with_verbose() -> None:
    quiet = _Terminal("y")
    _run(config.Plan(cloud="gcp", project="p", zone="us-central1-a"), quiet, _Commands())
    assert "Everything needed is in place" in quiet.text and "gcloud credentials" not in quiet.text
    loud = _Terminal("y")
    loud.io.verbose = True
    _run(config.Plan(cloud="gcp", project="p", zone="us-central1-a"), loud, _Commands())
    assert "gcloud credentials are valid" in loud.text


def test_without_gcloud_the_only_offer_is_a_machine_you_have() -> None:
    gone = FakeRunner([("gcloud --version", Result(1, "", ""))])
    term, commands = _Terminal("1", "deploy@203.0.113.7", ""), _Commands()
    plan = config.Plan()
    wizard.run(plan, name="muse", runner=gone, run_command=commands, io=term.io) if False else None
    # with no gcloud the SSH checks need a reachable machine: give the fake one
    gone.when("uname -s -m", stdout="Linux x86_64\n0\n")
    gone.when("registry-1.docker.io", stdout="docker=22\npypi=0\n")
    _run(plan, term, commands, runner=gone)
    assert plan.ssh == "deploy@203.0.113.7" and plan.cloud is None
    assert commands.calls[0][commands.calls[0].index("--ssh") + 1] == "deploy@203.0.113.7"
    assert "Google Cloud is not available" in term.text


def test_a_script_must_say_where_and_must_accept_the_cost() -> None:
    with pytest.raises(SystemExit, match="Say where"):
        _run(config.Plan(), _Terminal(interactive=False), _Commands())
    with pytest.raises(SystemExit, match="billable"):
        _run(config.Plan(cloud="gcp"), _Terminal(interactive=False), _Commands())
    commands = _Commands()
    _run(config.Plan(cloud="gcp", yes=True), _Terminal(interactive=False), commands)
    assert len(commands.calls) == 1  # no servers listed and nobody to ask: just the deployment


class _RejectsGithub(_Commands):
    def __call__(self, argv: list[str]) -> int:
        super().__call__(argv)
        return 1 if argv[:2] == ["add-server", "github"] else 0


def test_a_rejected_server_does_not_hide_that_the_install_worked() -> None:
    plan = config.Plan(
        cloud="gcp",
        yes=True,
        servers=[config.ServerPlan(server="github"), config.ServerPlan(server="learn")],
    )
    term, commands = _Terminal(interactive=False), _RejectsGithub()
    with pytest.raises(SystemExit) as stopped:
        _run(plan, term, commands)
    assert stopped.value.code == 1  # a script still sees that something needs attention
    assert [c[1] for c in commands.calls[1:]] == ["github", "learn"]  # the others were not skipped
    assert "installed and running" in term.text
    assert "muse-vm add-server github --name muse" in term.text
    assert "Not connected: learn" not in term.text


def test_declining_the_cost_creates_nothing() -> None:
    commands = _Commands()
    with pytest.raises(SystemExit, match="Nothing was created"):
        _run(config.Plan(cloud="gcp"), _Terminal("y", "n"), commands)  # settings, then the cost
    assert commands.calls == []


def test_a_failing_check_stops_before_anything_is_created() -> None:
    commands = _Commands()
    with pytest.raises(SystemExit, match="Fix the items"):
        _run(
            config.Plan(cloud="gcp", yes=True), _Terminal(), commands, runner=_gcloud(expired=True)
        )
    assert commands.calls == []


def test_every_setting_reaches_the_up_command() -> None:
    plan = config.Plan(
        cloud="gcp",
        project="p",
        zone="us-west1-b",
        machine_type="e2-medium",
        budget_usd=20,
        idle_stop_hours=8,
        tz="Europe/Paris",
        egress_allow=["10.0.0.0/24", "10.1.0.0/24"],
        yes=True,
    )
    commands = _Commands()
    _run(plan, _Terminal(interactive=False), commands)
    argv = commands.calls[0]
    for flag, value in (
        ("--project", "p"),
        ("--zone", "us-west1-b"),
        ("--machine-type", "e2-medium"),
        ("--budget-usd", "20"),
        ("--idle-stop-hours", "8"),
        ("--tz", "Europe/Paris"),
        ("--egress-allow", "10.0.0.0/24,10.1.0.0/24"),
    ):
        assert argv[argv.index(flag) + 1] == value


def test_a_failing_step_ends_the_run_with_its_exit_code() -> None:
    with pytest.raises(SystemExit) as stopped:
        _run(config.Plan(cloud="gcp", yes=True), _Terminal(interactive=False), _Commands(code=3))
    assert stopped.value.code == 3


def test_a_ready_deployment_is_kept_and_only_the_servers_are_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MUSE_VM_HOME", str(tmp_path))
    Deployment(
        name="muse", provider="gcp", ingress="tailscale-funnel", status="ready", hostname="a.ts.net"
    ).save()
    plan = config.Plan(cloud="gcp", yes=True, servers=[config.ServerPlan("learn")])
    term, commands = _Terminal(interactive=False), _Commands()
    _run(plan, term, commands)
    assert [call[0] for call in commands.calls] == ["add-server"]
    assert "already exists" in term.text and "https://a.ts.net" in term.text


def test_a_token_file_supplies_the_token_without_a_prompt(tmp_path: Path) -> None:
    token = tmp_path / "gh"
    token.write_text("github_pat_example\n")
    server = config.ServerPlan("github", token_file=str(token), allow=["issue_write"])
    plan = config.Plan(cloud="gcp", yes=True, servers=[server])
    commands = _Commands()
    _run(plan, _Terminal(interactive=False), commands)
    argv = commands.calls[1]
    assert os.environ["GITHUB_TOKEN"] == "github_pat_example"
    assert "github_pat_example" not in " ".join(argv)  # never on a command line
    assert argv[argv.index("--allow") + 1] == "issue_write"


def test_a_tailscale_key_file_is_used_and_a_bad_one_is_refused(tmp_path: Path) -> None:
    key = tmp_path / "ts"
    key.write_text("tskey-auth-kExample-abcdefghijklmnopqrstuvwx\n")
    commands = _Commands()
    _run(
        config.Plan(cloud="gcp", yes=True, tailscale_authkey_file=str(key)),
        _Terminal(interactive=False),
        commands,
    )
    assert os.environ["TS_AUTHKEY"].startswith("tskey-auth-")
    key.write_text("two\nlines\n")
    with pytest.raises(SystemExit, match="single non-empty line"):
        _run(
            config.Plan(cloud="gcp", yes=True, tailscale_authkey_file=str(key)),
            _Terminal(interactive=False),
            _Commands(),
        )


def test_a_mistyped_server_choice_is_asked_again_not_fatal() -> None:
    commands = _Commands()
    term = _Terminal("nonsense", "learn")  # a typo after the VM exists must not throw it away
    _run(config.Plan(cloud="gcp", yes=True, project="p", zone="us-central1-a"), term, commands)
    assert "Not in the list: nonsense" in term.text
    assert [c[:2] for c in commands.calls[1:]] == [["add-server", "learn"]]


def test_giving_up_on_the_server_choice_skips_servers() -> None:
    commands = _Commands()
    term = _Terminal("x", "y", "z")
    _run(config.Plan(cloud="gcp", yes=True), term, commands)
    assert len(commands.calls) == 1  # only the deployment


def test_a_server_of_your_own_can_be_given_by_address() -> None:
    commands = _Commands()
    term = _Terminal("https://mcp.example.com/mcp", "y")  # the address, then "needs a token"
    _run(config.Plan(cloud="gcp", yes=True, project="p", zone="us-central1-a"), term, commands)
    add = commands.calls[1]
    assert add[:3] == ["add-server", "mcp-example-com", "--name"]
    assert add[add.index("--endpoint") + 1] == "https://mcp.example.com/mcp"
    assert add[add.index("--credential") + 1] == "env_token"
    assert add[add.index("--secret-name") + 1] == "MCP_EXAMPLE_COM_TOKEN"


def test_a_yes_or_no_question_is_asked_again_on_a_stray_answer() -> None:
    term = _Terminal("1,2,4", "maybe", "n")
    assert term.io.confirm("Save?") is False
    assert "Please answer y or n." in term.text
    assert _Terminal("whatever", "x", "z").io.confirm("Save?", default=False) is False  # gives up


def test_typed_ahead_keys_are_discarded_before_a_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io as stdio
    import sys

    monkeypatch.setattr(sys, "stdin", stdio.StringIO("answer\n"))
    monkeypatch.setattr("builtins.input", lambda _q: "typed")
    assert wizard.ask_at_terminal("? ") == "typed"  # not a terminal: nothing to flush, no crash


def test_the_choices_can_be_saved_for_next_time(tmp_path: Path) -> None:
    target = tmp_path / config.FILENAME
    term = _Terminal("1", "y", "y", "learn", "y")  # where, settings, cost, servers, save
    _run(config.Plan(), term, _Commands(), save_to=target)
    saved = config.load(target)
    assert saved.cloud == "gcp" and saved.project == "my-proj"
    assert [s.server for s in saved.servers] == ["learn"]
    assert not any(secret in target.read_text() for secret in ("ghp_", "tskey", "github_pat"))


def test_setup_is_wired_into_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MUSE_VM_HOME", str(tmp_path / "state"))
    (tmp_path / config.FILENAME).write_text('cloud = "gcp"\nyes = true\nname = "prod"\n')
    seen: list[list[str]] = []
    monkeypatch.setattr(cli, "main", lambda argv=None: seen.append(list(argv or [])) or 0)
    monkeypatch.setattr(
        doctor, "gcloud_facts", lambda runner: doctor.GcpFacts("my-proj", None, "a@b")
    )
    monkeypatch.setattr(doctor, "gcp_checks", lambda report, runner, project, zone: None)
    args = cli.build_parser().parse_args(["setup", "--non-interactive", "--project", "flag-proj"])
    cli.cmd_setup(args)
    up = seen[0]
    assert up[:3] == ["up", "--name", "prod"] and up[up.index("--project") + 1] == "flag-proj"
    assert json.dumps(seen)  # nothing unserialisable leaked into the commands


# ---- installing on the machine you are on ------------------------------------------------------
def test_local_is_a_setting_and_reaches_the_up_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    plan = config.parse('local = true\nname = "vm"')
    assert plan.local and config.parse(config.dump(plan)).local
    commands = _Commands()
    _run(plan, _Terminal(interactive=False), commands, runner=_ssh_ok())
    assert "--local" in commands.calls[0] and "--ssh" not in commands.calls[0]


def _ssh_ok() -> FakeRunner:
    runner = FakeRunner()
    runner.when("uname -s -m", stdout="Linux x86_64\n0\n")
    runner.when("registry-1.docker.io", stdout="docker=22\npypi=0\n")
    return runner


def test_this_machine_is_offered_on_linux_but_never_inside_cloud_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wizard.sys, "platform", "linux")
    monkeypatch.delenv("CLOUD_SHELL", raising=False)
    gone = FakeRunner([("gcloud --version", Result(1, "", ""))])
    for answer, expected in (("1", "local"), ("2", "ssh")):
        plan = config.Plan()
        term = _Terminal(answer, "deploy@203.0.113.7")
        wizard._where(plan, term.io, gone)  # type: ignore[reportPrivateUsage]
        assert (plan.local, bool(plan.ssh)) == (
            (True, False) if expected == "local" else (False, True)
        )
        assert "This machine" in term.text

    monkeypatch.setenv("CLOUD_SHELL", "true")  # installing Docker into Cloud Shell would be wrong
    term = _Terminal("1", "deploy@203.0.113.7")
    wizard._where(config.Plan(), term.io, gone)  # type: ignore[reportPrivateUsage]
    assert "This machine" not in term.text


def test_the_doctor_checks_this_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    report = Report()
    doctor.local_machine_checks(report, _ssh_ok())
    assert report.ok
    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    mac = Report()
    doctor.local_machine_checks(mac, _ssh_ok())
    assert not mac.ok and "--ssh" in " ".join(mac.lines())


def test_a_blip_after_the_address_first_answers_does_not_abort_the_self_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from muse_vm import selfcheck

    calls: list[int] = []

    def flaky(method: str, url: str, **kwargs: object) -> tuple[int, dict[str, str], str]:
        calls.append(1)
        if len(calls) < 3:
            raise OSError("Name or service not known")
        return 200, {"cache-control": "no-store"}, "{}"

    monkeypatch.setattr(selfcheck, "_request", flaky)
    monkeypatch.setattr(selfcheck.time, "sleep", lambda _s: None)
    assert selfcheck._headers("https://x/health") == {"cache-control": "no-store"}  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(selfcheck, "_request", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    assert selfcheck._headers("https://x/health") == {}  # pyright: ignore[reportPrivateUsage]
