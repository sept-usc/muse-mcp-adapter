# pyright: basic, reportArgumentType=false
"""The owner password end to end: muse-vm's admin layer on stdin, into the real adapter command."""

from __future__ import annotations

from pathlib import Path

import pytest

from muse_vm import bundle
from muse_vm.admin import Admin
from muse_vm.runner import CommandError
from tests.fixtures.vm_fakes import ContainerTarget


def _vm(tmp_path: Path) -> ContainerTarget:
    vm = ContainerTarget(tmp_path / "vm")
    vm.put("config/serve.json", bundle.serve_config("adapter.example.ts.net", chat_setup=True))
    return vm


def test_a_password_sent_on_stdin_is_stored_and_never_echoed(tmp_path: Path) -> None:
    admin = Admin(_vm(tmp_path))
    admin.json("chat-setup-enable")
    answer = admin.json("chat-setup-set-password", stdin="a good long password\n")
    assert answer["ownerPasswordSet"] is True
    assert "a good long password" not in str(answer)
    # only a salted hash is kept, marked as a password
    import sqlite3

    db = sqlite3.connect(tmp_path / "vm" / "data" / "adapter.db")
    salt = db.execute("SELECT code_salt FROM owner_credential").fetchone()[0]
    db.close()
    assert salt.startswith("pw1:")
    assert "a good long password" not in (
        tmp_path / "vm" / "data" / "adapter.db"
    ).read_bytes().decode("latin-1")


@pytest.mark.parametrize("bad", ["", "short\n", "x" * 201 + "\n", "tab\tinside the pass\n"])
def test_a_bad_password_is_refused_and_the_old_one_stays(tmp_path: Path, bad: str) -> None:
    admin = Admin(_vm(tmp_path))
    admin.json("chat-setup-enable")
    with pytest.raises(CommandError):
        admin.json("chat-setup-set-password", stdin=bad)
    import sqlite3

    db = sqlite3.connect(tmp_path / "vm" / "data" / "adapter.db")
    salt = db.execute("SELECT code_salt FROM owner_credential").fetchone()[0]
    db.close()
    assert not salt.startswith("pw1:")  # the generated code is still the owner credential
