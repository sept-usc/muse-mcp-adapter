"""muse-vm against the real adapter: connecting a server, the self-check, and `up` as a whole."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from muse_mcp_adapter.application.secret_source import SecretSource
from muse_mcp_adapter.application.serve import build_application, load_serve_config
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from muse_vm import bundle, cli, ingress, selfcheck
from muse_vm.admin import Admin
from muse_vm.providers.base import Provider
from muse_vm.selfcheck import Report
from muse_vm.state import Deployment
from tests.fixtures.live_server import free_port, run_asgi
from tests.fixtures.vm_fakes import ContainerTarget, FakeTarget
from tests.validation.env import atlas_server

pytestmark = pytest.mark.validation


def _returning(value: Any) -> Any:
    def call(*_args: Any, **_kwargs: Any) -> Any:
        return value

    return call


def _fake_for(vm: Any) -> Any:
    def make(dep: Deployment, *_args: Any, **_kwargs: Any) -> Any:
        return _FakeProvider(dep, vm)

    return make


class _FakeProvider(Provider):
    """A provider whose machine is a folder on this computer running the real adapter commands."""

    name = "fake"

    def __init__(self, dep: Deployment, target: Any, *, healthy: bool = True) -> None:
        super().__init__(dep, lambda *a, **k: None, lambda _: None, {})  # type: ignore[arg-type]
        self._target = target
        self.torn_down = False
        self.started = False

    def target(self) -> Any:
        return self._target

    def start_stack(self, t: Any) -> None:
        self.started = True

    def teardown(self, keep_data: bool = False) -> None:
        self.torn_down = True


def _use(monkeypatch: pytest.MonkeyPatch, provider: _FakeProvider) -> None:
    monkeypatch.setattr(cli, "provider_for", _returning(provider))


def _config(directory: Path) -> None:
    """The adapter's configuration as the VM has it, with the secrets folder on this computer."""
    (directory / "config").mkdir(parents=True, exist_ok=True)
    (directory / "config" / "serve.json").write_text(
        json.dumps(
            {
                "publicUrl": "https://adapter.tail1.ts.net",
                "secretsDir": str(directory / "secrets"),
                "limits": {"rateLimit": 1000},
                # only because the stand-in MCP server here is on loopback over plain HTTP; the
                # configuration muse-vm writes for a real VM sets neither
                "allowPrivateNetworks": True,
                "allowInsecureHttp": True,
            }
        )
    )


def _fails(argv: list[str], capsys: pytest.CaptureFixture[str], message: str) -> None:
    """The command stops with a non-zero code and says why."""
    assert cli.main(argv) == 1
    assert message in capsys.readouterr().err


def _output(capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    text = capsys.readouterr().out

    def pick(label: str) -> re.Match[str] | None:
        return re.search(rf"{label}\s+(\S+)", text)

    return {
        "url": pick("Connector URL").group(1),  # type: ignore[union-attr]
        "token": pick("Token").group(1),  # type: ignore[union-attr]
        "text": text,
    }


# ---- connecting a real MCP server and using the connector it creates ------------------------
def test_add_server_connects_a_real_server_and_the_printed_token_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MUSE_VM_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("TRACKER_TOKEN", "atlas-secret-token")
    Deployment(
        name="muse", provider="fake", ingress="tailscale-funnel", hostname="adapter.tail1.ts.net"
    ).save()
    vm = ContainerTarget(tmp_path / "vm")
    _config(tmp_path / "vm")
    _use(monkeypatch, _FakeProvider(Deployment.load("muse"), vm))

    with atlas_server(auth="bearer") as atlas:
        code = cli.main(
            [
                "add-server",
                "tracker",
                "--endpoint",
                atlas.url,
                "--credential",
                "env_token",
                "--secret-name",
                "TRACKER_TOKEN",
                "--allow",
                "create_issue",
                "--dev-network",
                "--no-copy",
                "--name",
                "muse",
            ]
        )
        assert code == 0
        shown = _output(capsys)
        assert "exposed to Muse" in shown["text"] and "tool:create_issue [write]" in shown["text"]
        assert "tool:get_issue [read]" in shown["text"]
        assert "tool:delete_issue" not in shown["text"]  # a write nobody allowed stays hidden

        # the secret went to the adapter's secrets folder, readable by it alone, not into the state
        stored = tmp_path / "vm" / "secrets" / "TRACKER_TOKEN"
        assert stored.read_text().strip() == "atlas-secret-token"
        assert oct(stored.stat().st_mode & 0o777) == "0o400"
        assert "atlas-secret-token" not in Deployment.path_for("muse").read_text()

        # now be Muse: the printed connector URL and token against the real running adapter
        config = load_serve_config(tmp_path / "vm" / "config" / "serve.json")
        repository = SqliteControlPlaneRepository(tmp_path / "vm" / "data" / "adapter.db")
        repository.initialize()
        application = build_application(
            repository,
            config,
            SecretSource({}, tmp_path / "vm" / "secrets"),
            database=tmp_path / "vm" / "data" / "adapter.db",
        )
        slug = shown["url"].split("/c/")[1].split("/")[0]
        headers = {"Authorization": f"Bearer {shown['token']}"}
        with TestClient(application, base_url="https://34-1-2-3.sslip.io") as client:
            spec = client.get(f"/c/{slug}/openapi.json").json()
            names = {path.rsplit("/", 1)[-1] for path in spec["paths"] if "/operations/" in path}
            assert {"get_issue", "create_issue"} <= names and "delete_issue" not in names

            read = client.post(
                f"/c/{slug}/operations/get_issue", json={"issue_id": "ISSUE-1"}, headers=headers
            )
            assert read.status_code == 200, read.text  # the secret reached Atlas as its bearer

            denied = client.post(f"/c/{slug}/operations/get_issue", json={"issue_id": "ISSUE-1"})
            assert denied.status_code == 401

            write = client.post(
                f"/c/{slug}/operations/create_issue",
                json={"title": "from muse-vm"},
                headers={**headers, "Idempotency-Key": "k1"},
            )
            assert write.status_code == 200
        assert [e["tool"] for e in atlas.effects()] == ["create_issue"]  # exactly one real effect
        repository.close()

    deployment = Deployment.load("muse")
    assert deployment.servers == ["tracker"]


def test_add_server_for_a_public_server_needs_no_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MUSE_VM_HOME", str(tmp_path / "state"))
    Deployment(
        name="muse", provider="fake", ingress="tailscale-funnel", hostname="adapter.tail1.ts.net"
    ).save()
    vm = ContainerTarget(tmp_path / "vm")
    _config(tmp_path / "vm")
    _use(monkeypatch, _FakeProvider(Deployment.load("muse"), vm))
    with atlas_server() as atlas:
        assert (
            cli.main(
                [
                    "add-server",
                    "open-tracker",
                    "--endpoint",
                    atlas.url,
                    "--dev-network",
                    "--no-copy",
                    "--name",
                    "muse",
                ]
            )
            == 0
        )
    shown = _output(capsys)
    assert shown["token"].startswith("mcpa_") and "/openapi.json" in shown["url"]
    assert not list((tmp_path / "vm" / "secrets").iterdir())  # nothing to store


def test_naming_a_tool_the_server_does_not_have_stops_before_anything_is_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MUSE_VM_HOME", str(tmp_path / "state"))
    Deployment(
        name="muse", provider="fake", ingress="tailscale-funnel", hostname="adapter.tail1.ts.net"
    ).save()
    vm = ContainerTarget(tmp_path / "vm")
    _config(tmp_path / "vm")
    _use(monkeypatch, _FakeProvider(Deployment.load("muse"), vm))
    with atlas_server() as atlas:
        _fails(
            [
                "add-server",
                "tracker",
                "--endpoint",
                atlas.url,
                "--allow",
                "launch_missiles",
                "--dev-network",
                "--name",
                "muse",
            ],
            capsys,
            "no such tool",
        )
    admin = Admin(vm, dev_network=True)
    assert admin.status("tracker")["activeCatalog"] is None  # nothing went live


def test_a_server_id_that_is_not_valid_is_refused_before_it_is_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MUSE_VM_HOME", str(tmp_path / "state"))
    Deployment(
        name="muse", provider="fake", ingress="tailscale-funnel", hostname="adapter.tail1.ts.net"
    ).save()
    vm = ContainerTarget(tmp_path / "vm")
    _config(tmp_path / "vm")
    _use(monkeypatch, _FakeProvider(Deployment.load("muse"), vm))
    _fails(
        ["add-server", "Bad_ID", "--endpoint", "https://x.example.com/mcp", "--name", "muse"],
        capsys,
        "server id",
    )
    assert not list((tmp_path / "vm" / "secrets").iterdir())  # nothing was stored first


# ---- the self-check against a real adapter --------------------------------------------------
def _inside_ok(target: FakeTarget) -> None:
    target.answer("nft list table", stdout="set block4 { }")
    canary = {
        "169.254.169.254": "ECONNREFUSED",
        "10.0.0.1": "timeout",
        "172.16.0.1": "timeout",
        "192.168.0.1": "EHOSTUNREACH",
        "100.64.0.1": "timeout",
        "public": "ok",
    }
    target.answer("python -", stdout=json.dumps(canary))
    target.answer("id -u", stdout="10001\n")
    target.answer("touch /probe", stdout="read-only\n")
    target.answer("stat -c", stdout="700 700\n")


def test_the_self_check_passes_against_a_real_adapter(tmp_path: Path) -> None:
    repository = SqliteControlPlaneRepository(tmp_path / "a.db")
    repository.initialize()
    config = load_serve_config(_write(tmp_path, {"publicUrl": "https://adapter.example.com"}))
    application = build_application(repository, config, {}, database=tmp_path / "a.db")
    target = FakeTarget()
    _inside_ok(target)
    with run_asgi(application, free_port()) as port:
        report = selfcheck.run(target, f"http://127.0.0.1:{port}", wait=20)
    assert report.ok, report.lines()
    names = [name for name, *_ in report.checks]
    assert (
        "a call without a token is refused" in names
        and "a call with a wrong token is refused" in names
    )
    repository.close()


def test_the_self_check_fails_when_the_adapter_answers_a_call_without_a_token() -> None:
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def anything(_: Any) -> JSONResponse:
        return JSONResponse({"status": "ok"})  # answers everything, including /health and calls

    app = Starlette(routes=[Route("/{path:path}", anything, methods=["GET", "POST"])])
    target = FakeTarget()
    _inside_ok(target)
    with run_asgi(app, free_port()) as port:
        report = selfcheck.run(target, f"http://127.0.0.1:{port}", wait=20)
    assert not report.ok
    failed = [name for name, ok, _, required in report.checks if required and not ok]
    assert "a call without a token is refused" in failed


def test_the_self_check_fails_when_the_metadata_server_is_reachable_from_the_adapter() -> None:
    target = FakeTarget()
    _inside_ok(target)
    leaky = {
        "169.254.169.254": "CONNECTED",
        "10.0.0.1": "timeout",
        "172.16.0.1": "timeout",
        "192.168.0.1": "timeout",
        "100.64.0.1": "timeout",
        "public": "ok",
    }
    target.answers.insert(
        0,
        (
            "python -",
            __import__("muse_vm.runner", fromlist=["Result"]).Result(0, json.dumps(leaky), ""),
        ),
    )
    report = selfcheck.inside_checks(target)
    assert not report.ok
    assert any(
        "reachable: 169.254.169.254" in detail for _, ok, detail, _ in report.checks if not ok
    )


def test_the_self_check_notices_a_root_or_writable_container() -> None:
    target = FakeTarget()
    _inside_ok(target)
    target.answers.insert(
        0, ("id -u", __import__("muse_vm.runner", fromlist=["Result"]).Result(0, "0\n", ""))
    )
    target.answers.insert(
        0,
        (
            "touch /probe",
            __import__("muse_vm.runner", fromlist=["Result"]).Result(0, "writable\n", ""),
        ),
    )
    failed = [n for n, ok, _, req in selfcheck.inside_checks(target).checks if req and not ok]
    assert "the adapter runs as an unprivileged user" in failed
    assert "the adapter's filesystem is read-only" in failed


def test_the_self_check_gives_up_waiting_with_the_reason() -> None:
    healthy, why = selfcheck.wait_healthy(
        f"http://127.0.0.1:{free_port()}", timeout=0.3, say=lambda _: None, sleep=lambda _: None
    )
    assert not healthy and why


def _write(directory: Path, data: dict[str, Any]) -> Path:
    path = directory / "serve.json"
    path.write_text(json.dumps(data))
    return path


# ---- `up` as a whole -------------------------------------------------------------------------
def _up_args(*extra: str) -> list[str]:
    return ["up", "--cloud", "gcp", "--yes", "--allow-dirty", "--name", "muse", *extra]


def test_up_installs_the_bundle_starts_it_and_leaves_a_ready_deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MUSE_VM_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(selfcheck, "run", _returning(_passing()))
    vm = ContainerTarget(tmp_path / "vm")
    holder: dict[str, _FakeProvider] = {}

    def make(dep: Deployment, runner: Any, say: Any, opts: Any) -> _FakeProvider:
        holder["p"] = _FakeProvider(dep, vm)
        return holder["p"]

    monkeypatch.setattr(cli, "provider_for", make)
    monkeypatch.setattr(ingress, "hostname_after_start", _returning("adapter.tail1.ts.net"))
    assert cli.main(_up_args()) == 0

    dep = Deployment.load("muse")
    assert dep.status == "ready" and dep.hostname == "adapter.tail1.ts.net"
    assert dep.ingress == "tailscale-funnel"
    assert holder["p"].started and not holder["p"].torn_down
    for name in (
        "compose.yml",
        "tailscale-serve.json",
        "config/serve.json",
        ".env",
        "backup.sh",
        "heal.sh",
    ):
        assert (tmp_path / "vm" / name).exists(), name
    assert oct((tmp_path / "vm" / ".env").stat().st_mode & 0o777) == "0o600"
    env = bundle.parse_env((tmp_path / "vm" / ".env").read_text())
    assert "COMPOSE_PROFILES" not in env and "TS_AUTHKEY" not in env
    config = json.loads((tmp_path / "vm" / "config" / "serve.json").read_text())
    assert config["publicUrl"] == "https://adapter.tail1.ts.net"
    assert "muse-vm add-server" in capsys.readouterr().out  # it says what to do next


def test_up_rolls_back_everything_when_the_self_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MUSE_VM_HOME", str(tmp_path / "state"))
    failing = Report()
    failing.add("a call without a token is refused", False, "HTTP 200")
    monkeypatch.setattr(selfcheck, "run", _returning(failing))
    vm = ContainerTarget(tmp_path / "vm")
    holder: dict[str, _FakeProvider] = {}

    def make(dep: Deployment, runner: Any, say: Any, opts: Any) -> _FakeProvider:
        holder["p"] = _FakeProvider(dep, vm)
        return holder["p"]

    monkeypatch.setattr(cli, "provider_for", make)
    monkeypatch.setattr(ingress, "hostname_after_start", _returning("adapter.tail1.ts.net"))
    _fails(_up_args(), capsys, "self-check failed")
    assert holder["p"].torn_down is True
    assert Deployment.all() == []  # no half-made deployment is left in the state


def test_up_keeps_a_failed_deployment_when_asked_to_for_debugging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MUSE_VM_HOME", str(tmp_path / "state"))
    failing = Report()
    failing.add("public HTTPS /health answers", False, "timeout")
    monkeypatch.setattr(selfcheck, "run", _returning(failing))
    vm = ContainerTarget(tmp_path / "vm")
    provider_holder: dict[str, _FakeProvider] = {}

    def make(dep: Deployment, runner: Any, say: Any, opts: Any) -> _FakeProvider:
        provider_holder["p"] = _FakeProvider(dep, vm)
        return provider_holder["p"]

    monkeypatch.setattr(cli, "provider_for", make)
    monkeypatch.setattr(ingress, "hostname_after_start", _returning("adapter.tail1.ts.net"))
    assert cli.main(_up_args("--keep-on-failure")) == 1
    capsys.readouterr()
    assert provider_holder["p"].torn_down is False
    assert Deployment.load("muse").status == "failed"


def test_up_refuses_a_name_that_is_already_in_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MUSE_VM_HOME", str(tmp_path / "state"))
    Deployment(name="muse", provider="gcp", ingress="tailscale-funnel").save()
    _fails(_up_args(), capsys, "already exists")


def test_up_needs_to_be_told_where_to_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MUSE_VM_HOME", str(tmp_path / "state"))
    _fails(["up", "--name", "muse"], capsys, "--ssh user@host or --cloud gcp")


def test_a_tailscale_auth_key_goes_into_the_private_environment_file_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MUSE_VM_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("TS_AUTHKEY", "tskey-auth-kExampleKey123-abcdefghijklmnopqrstuvwx")
    monkeypatch.setattr(selfcheck, "run", _returning(_passing()))
    monkeypatch.setattr(ingress, "hostname_after_start", _returning("adapter.tail1.ts.net"))
    vm = ContainerTarget(tmp_path / "vm")
    monkeypatch.setattr(cli, "provider_for", _fake_for(vm))
    assert cli.main(_up_args()) == 0
    env_file = tmp_path / "vm" / ".env"
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"
    # it is needed only to join the tailnet: once the device has joined it is removed again
    assert "TS_AUTHKEY" not in env_file.read_text()
    assert "tskey" not in env_file.read_text()
    shown = capsys.readouterr()
    assert "tskey-auth-kExampleKey123-abcdefghijklmnopqrstuvwx" not in shown.out + shown.err
    assert "tskey" not in Deployment.path_for("muse").read_text()  # not remembered on this computer


def _passing() -> Report:
    report = Report()
    report.add("public HTTPS /health answers", True)
    return report


def test_a_malformed_tailscale_auth_key_is_refused_before_anything_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MUSE_VM_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("TS_AUTHKEY", "tailx tskey-auth-kExampleKey123-abcdefghijklmnopqrstuvwx")
    monkeypatch.setattr(cli, "provider_for", _returning(None))
    _fails(_up_args(), capsys, "does not look like a Tailscale auth key")
    assert Deployment.all() == []
