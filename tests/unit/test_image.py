"""The container image and its dependency lock, checked without needing Docker."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "Dockerfile").read_text()
LOCK = (ROOT / "requirements.lock").read_text()


def _lock_blocks() -> dict[str, str]:
    blocks: dict[str, str] = {}
    current = None
    for line in LOCK.splitlines():
        match = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s\\]+)", line)
        if match:
            current = match.group(1).lower().replace("_", "-")
            blocks[current] = line
        elif current:
            blocks[current] += "\n" + line
    return blocks


def test_the_base_image_is_pinned_by_digest() -> None:
    images = re.findall(r"^ARG PYTHON_IMAGE=(\S+)", DOCKERFILE, re.M)
    assert images and all(re.search(r"@sha256:[0-9a-f]{64}$", image) for image in images)
    assert re.findall(r"^FROM (\S+)", DOCKERFILE, re.M) == ["${PYTHON_IMAGE}"] * 2


def test_nothing_floats_to_the_latest_version() -> None:
    assert ":latest" not in DOCKERFILE


def test_the_image_runs_as_an_unprivileged_numeric_user() -> None:
    assert re.findall(r"^USER (\S+)", DOCKERFILE, re.M) == ["10001:10001"]


def test_the_image_checks_its_own_health_and_declares_its_state_volume() -> None:
    assert "HEALTHCHECK" in DOCKERFILE and "/health" in DOCKERFILE
    assert re.search(r"^VOLUME /data$", DOCKERFILE, re.M)


def test_dependencies_install_only_with_matching_hashes() -> None:
    assert "--require-hashes" in DOCKERFILE and "--no-deps" in DOCKERFILE


def test_nothing_is_fetched_by_piping_a_download_into_a_shell() -> None:
    assert not re.search(r"(curl|wget)[^\n]*\|\s*(sh|bash)", DOCKERFILE)


def test_only_the_adapter_and_its_lock_go_into_the_image() -> None:
    copies = re.findall(r"^COPY (?!--from)(\S+) (\S+)", DOCKERFILE, re.M)
    assert {source for source, _ in copies} == {"requirements.lock", "src/muse_mcp_adapter"}
    assert all((ROOT / source).exists() for source, _ in copies)


def test_every_locked_package_is_exact_and_has_hashes() -> None:
    blocks = _lock_blocks()
    assert len(blocks) > 20
    for name, block in blocks.items():
        assert "--hash=sha256:" in block, f"{name} has no hash"


def test_the_lock_covers_every_direct_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    wanted = {
        re.split(r"[<>=!~\[ ]", item)[0].lower().replace("_", "-")
        for item in project["dependencies"]
    }
    assert wanted <= set(_lock_blocks())


def test_secrets_and_state_never_enter_the_build_context() -> None:
    ignored = (ROOT / ".dockerignore").read_text().split()
    for pattern in (".git", ".internal", ".venv", "*.key", "*.db", ".env", "tests"):
        assert pattern in ignored
