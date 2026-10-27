# pyright: basic
"""The vault of sealed values and the hash-chained ledger of releases."""

import sqlite3
from pathlib import Path

import pytest

from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository
from muse_mcp_adapter.persistence.vault_store import GENESIS, HandleVault, ReleaseLedger

T0 = "2026-10-26T12:00:00Z"
T1 = "2026-10-26T12:30:00Z"
NEXT_DAY = "2026-10-27T12:15:00Z"


@pytest.fixture
def stores(tmp_path: Path):
    with SqliteControlPlaneRepository(tmp_path / "a.db") as repository:
        repository.initialize()
        yield (
            HandleVault(repository, HandleVault.new_key()),
            ReleaseLedger(repository),
            tmp_path / "a.db",
        )


def test_a_value_comes_back_from_its_handle(stores) -> None:
    vault, _, _ = stores
    token = vault.handle_for("c1", "contact.email", "dana@x.test", T0)
    assert token.startswith("[contact.email:") and token.endswith("]")
    handle_id = token.split(":")[1].rstrip("]")
    assert len(handle_id) == 8
    assert vault.lookup("c1", handle_id, T1) == ("contact.email", "dana@x.test")


def test_the_same_value_always_gets_the_same_handle(stores) -> None:
    vault, _, _ = stores
    a = vault.handle_for("c1", "contact.email", "dana@x.test", T0)
    assert vault.handle_for("c1", "contact.email", "dana@x.test", T1) == a
    assert vault.handle_for("c1", "contact.email", "lee@x.test", T0) != a
    assert vault.handle_for("c1", "contact.phone", "dana@x.test", T0) != a  # label is part of it


def test_a_handle_is_useless_to_another_connection(stores) -> None:
    vault, _, _ = stores
    token = vault.handle_for("c1", "contact.email", "dana@x.test", T0)
    handle_id = token.split(":")[1].rstrip("]")
    assert vault.lookup("c2", handle_id, T0) is None
    other = vault.handle_for("c2", "contact.email", "dana@x.test", T0)
    assert other != token  # the same value is a different handle for another connection


def test_handles_expire_and_reading_again_renews_them(stores) -> None:
    vault, _, _ = stores
    token = vault.handle_for("c1", "contact.email", "dana@x.test", T0)
    handle_id = token.split(":")[1].rstrip("]")
    assert vault.lookup("c1", handle_id, NEXT_DAY) is None
    vault.handle_for("c1", "contact.email", "dana@x.test", T1)
    assert vault.lookup("c1", handle_id, NEXT_DAY) is not None


def test_values_are_encrypted_at_rest(stores) -> None:
    vault, _, database = stores
    vault.handle_for("c1", "contact.email", "dana.secret@x.test", T0)
    raw = sqlite3.connect(database).execute("SELECT * FROM handles").fetchall()
    assert "dana.secret" not in str(raw)


def test_a_different_key_cannot_read_the_values(stores, tmp_path) -> None:
    vault, _, database = stores
    token = vault.handle_for("c1", "contact.email", "dana@x.test", T0)
    handle_id = token.split(":")[1].rstrip("]")
    with SqliteControlPlaneRepository(database) as repository:
        stranger = HandleVault(repository, HandleVault.new_key())
        assert stranger.lookup("c1", handle_id, T0) is None


def test_a_bad_key_length_is_refused(stores) -> None:
    with pytest.raises(ValueError):
        HandleVault(stores[0]._repository, b"short")


def test_expired_handles_are_purged_and_unlinking_forgets_everything(stores) -> None:
    vault, _, _ = stores
    old = vault.handle_for("c1", "contact.email", "a@x.test", T0)
    fresh = vault.handle_for("c1", "contact.email", "b@x.test", NEXT_DAY)
    assert vault.purge_expired(NEXT_DAY) == 1
    assert vault.lookup("c1", old.split(":")[1].rstrip("]"), T0) is None
    vault.revoke_connection("c1")
    assert vault.lookup("c1", fresh.split(":")[1].rstrip("]"), NEXT_DAY) is None


# --- the ledger -----------------------------------------------------------------------------


def add(ledger: ReleaseLedger, kind="sealed", label="contact.email", detail="h", now=T0) -> None:
    ledger.record(
        kind, connection_id="c1", operation="mail_list", label=label, detail=detail, now=now
    )


def test_entries_are_kept_in_order_and_chained(stores) -> None:
    _, ledger, _ = stores
    add(ledger, "sealed")
    add(ledger, "rehydrated", detail="h into to")
    add(ledger, "refused", detail="not allowed in body")
    entries = ledger.entries()
    assert [e.kind for e in entries] == ["sealed", "rehydrated", "refused"]
    assert [e.seq for e in entries] == [1, 2, 3]
    assert len({e.entry_hash for e in entries}) == 3
    assert ledger.verify() == (True, None)


def test_an_empty_ledger_is_valid(stores) -> None:
    assert stores[1].verify() == (True, None)
    assert GENESIS == "0" * 64


def test_changing_an_entry_is_detected_and_located(stores) -> None:
    _, ledger, database = stores
    for number in range(4):
        add(ledger, detail=f"d{number}")
    with sqlite3.connect(database) as db:
        db.execute("UPDATE release_ledger SET detail = 'changed' WHERE seq = 2")
    assert ledger.verify() == (False, 2)


def test_deleting_an_entry_is_detected(stores) -> None:
    _, ledger, database = stores
    for number in range(4):
        add(ledger, detail=f"d{number}")
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM release_ledger WHERE seq = 2")
    ok, where = ledger.verify()
    assert not ok and where == 3


def test_removing_the_last_entries_is_not_detectable_but_inserting_is(stores) -> None:
    _, ledger, database = stores
    for number in range(3):
        add(ledger, detail=f"d{number}")
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO release_ledger (occurred_at, connection_id, operation, kind, label, "
            "detail, prev_hash, entry_hash) VALUES ('t', 'c', 'o', 'sealed', 'l', 'forged', "
            "'bogus', 'bogus')"
        )
    assert ledger.verify()[0] is False


def test_the_summary_counts_by_kind_and_label_and_can_start_from_a_time(stores) -> None:
    _, ledger, _ = stores
    add(ledger, "sealed", "contact.email", now=T0)
    add(ledger, "sealed", "contact.email", now=T1)
    add(ledger, "withheld", "contact.notes", now=T1)
    assert ledger.summary() == {"sealed": {"contact.email": 2}, "withheld": {"contact.notes": 1}}
    assert ledger.summary(since=T1) == {
        "sealed": {"contact.email": 1},
        "withheld": {"contact.notes": 1},
    }


def test_the_ledger_never_holds_a_value(stores) -> None:
    vault, ledger, database = stores
    token = vault.handle_for("c1", "contact.email", "very.secret@x.test", T0)
    add(ledger, detail=token)
    raw = sqlite3.connect(database).execute("SELECT * FROM release_ledger").fetchall()
    assert "very.secret" not in str(raw)
