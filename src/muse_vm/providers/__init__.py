from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..runner import Runner
from ..state import Deployment
from .base import Provider
from .gcp import GcpProvider
from .local import LocalProvider
from .ssh import SSHProvider

PROVIDERS: dict[str, type[Provider]] = {
    "ssh": SSHProvider,
    "gcp": GcpProvider,
    "local": LocalProvider,
}


def provider_for(
    dep: Deployment, runner: Runner, say: Callable[[str], None], opts: dict[str, Any]
) -> Provider:
    if dep.provider not in PROVIDERS:
        raise SystemExit(f"Unknown provider {dep.provider!r}.")
    return PROVIDERS[dep.provider](dep, runner, say, opts)
