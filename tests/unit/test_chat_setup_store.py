"""The storage under chat-driven setup: proposals, the owner's code, secrets and the audit chain."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from muse_mcp_adapter.chat_setup import owner
from muse_mcp_adapter.chat_setup.secrets import ChainedSecrets, SecretBox
from muse_mcp_adapter.chat_setup.store import SetupStore
from muse_mcp_adapter.connections import ConnectionService
from muse_mcp_adapter.domain.errors import ConflictError, EntityNotFoundError
from muse_mcp_adapter.domain.models import ServerRegistration
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository

NOW = "2026-10-05T10:00:00Z"
LATER = "2026-10-05T11:00:00Z"


def _store(tmp_path: Path) -> tuple[SetupStore, SqliteControlPlaneRepository, str]:
    repository = SqliteControlPlaneRepository(tmp_path / "adapter.db")
    repository.initialize()
    repository.create_server(
        ServerRegistration(
            "adapter-setup", "Setup", "https://setup.invalid/b", "none", None, True, NOW, NOW
        )
    )
    linked = ConnectionService(repository).link(
        server_id="adapter-setup", label="setup", credential_kind="none"
    )
    return (
        SetupStore(repository, SecretBox(SecretBox.generate_key())),
        repository,
        linked.record.connection_id,
    )


def _open(store: SetupStore, connection: str, number: int = 1, **fields: object) -> str:
    proposal_id = f"prop_{number:012x}"
    store.create_proposal(
        proposal_id=proposal_id,
        requester_connection_id=connection,
        kind="connect",
        user_words="words",
        request={"userWords": "words", "n": number},
        token_hash=f"hash{number}",
        now=NOW,
        expires_at=LATER,
        **fields,  # type: ignore[arg-type]
    )
    return proposal_id


def test_proposals_move_only_from_the_state_a_step_expects(tmp_path: Path) -> None:
    store, repository, connection = _store(tmp_path)
    with repository:
        proposal_id = _open(store, connection, target_server_id="learn")
        stored = store.get_proposal(proposal_id)
        assert stored.state == "waiting_for_owner" and stored.target_server_id == "learn"
        assert store.find_by_token("hash1") == stored and store.find_by_token("nope") is None

        moved = store.transition(
            proposal_id,
            expected=("waiting_for_owner",),
            state="in_review",
            now=NOW,
            computed={"digest": "d"},
            digest="d",
            snapshot_id=3,
            decision={"a": 1},
            applied={"b": 2},
            error="boom",
            target_connection_id="conn_x",
        )
        assert (moved.state, moved.digest, moved.snapshot_id) == ("in_review", "d", 3)
        assert moved.computed == {"digest": "d"} and moved.decision == {"a": 1}
        with pytest.raises(ConflictError):
            store.transition(proposal_id, expected=("waiting_for_owner",), state="applied", now=NOW)
        with pytest.raises(ValueError):
            store.transition(
                proposal_id, expected=("in_review",), state="applied", now=NOW, bogus=1
            )
        with pytest.raises(EntityNotFoundError):
            store.get_proposal("prop_missing")


def test_open_proposals_are_counted_listed_deduplicated_and_expired(tmp_path: Path) -> None:
    store, repository, connection = _store(tmp_path)
    with repository:
        first, second = _open(store, connection, 1), _open(store, connection, 2)
        assert store.count_open(connection) == 2
        duplicate = store.find_open_duplicate(connection, "connect", {"userWords": "words", "n": 1})
        assert duplicate is not None and duplicate.proposal_id == first
        assert store.find_open_duplicate(connection, "connect", {"n": 99}) is None
        assert {p.proposal_id for p in store.list_proposals()} == {first, second}
        assert [p.proposal_id for p in store.list_proposals(states=("applied",))] == []

        store.reissue_token(first, "freshhash", NOW)
        assert store.find_by_token("hash1") is None and store.find_by_token("freshhash")
        assert store.expire_due(NOW) == []
        expired = store.expire_due(LATER)
        assert {p.proposal_id for p in expired} == {first, second}
        assert store.get_proposal(first).state == "expired" and store.count_open(connection) == 0
        with pytest.raises(ConflictError):
            store.reissue_token(first, "again", LATER)


def test_the_connector_token_is_kept_sealed_and_forgotten_on_time(tmp_path: Path) -> None:
    store, repository, connection = _store(tmp_path)
    with repository:
        proposal_id = _open(store, connection)
        store.transition(
            proposal_id, expected=("waiting_for_owner",), state="applied", now=NOW, reveal="secret!"
        )
        assert store.get_proposal(proposal_id).has_reveal
        assert store.reveal_text(proposal_id) == "secret!"
        raw = repository.run(
            lambda db: db.execute("SELECT reveal_ciphertext FROM setup_proposals").fetchone()[0]
        )
        assert "secret!" not in raw

        store.wipe_old_reveals("2026-10-05T09:00:00Z")  # not yet old enough
        assert store.reveal_text(proposal_id) == "secret!"
        store.wipe_old_reveals(LATER)
        assert (
            store.reveal_text(proposal_id) is None
            and not store.get_proposal(proposal_id).has_reveal
        )
        assert store.reveal_text("prop_missing") is None


def test_the_owner_code_sessions_attempts_and_secrets(tmp_path: Path) -> None:
    store, repository, _ = _store(tmp_path)
    with repository:
        assert store.owner_code() is None
        salt = owner.new_salt()
        store.set_owner_code(owner.hash_code("abcd-efgh-jkmn", salt), salt, NOW)
        stored = store.owner_code()
        assert stored is not None
        assert owner.matches(
            "ABCD EFGH JKMN", stored[1], stored[0]
        )  # spaces and case do not matter
        assert not owner.matches("ABCD-EFGH-JKMP", stored[1], stored[0])
        store.set_owner_code("h2", "s2", LATER)  # replaced, never duplicated
        assert store.owner_code() == ("h2", "s2")

        store.add_session("s1", NOW, LATER)
        assert store.session_valid("s1", NOW) and not store.session_valid("s1", LATER)
        assert not store.session_valid("other", NOW)
        store.drop_sessions(LATER)
        store.add_session("s2", NOW, LATER)
        store.drop_sessions()
        assert not store.session_valid("s2", NOW)

        for _ in range(3):
            store.add_attempt("proposal:x", NOW)
        assert store.count_attempts("proposal:x", "2026-10-05T09:00:00Z") == 3
        assert store.count_attempts("proposal:x", LATER) == 0
        store.clear_attempts("proposal:x")
        assert store.count_attempts("proposal:x", "2026-10-05T09:00:00Z") == 0

        store.put_secret("CHAT_SETUP_X", "tok", NOW)
        store.put_secret("CHAT_SETUP_X", "tok2", LATER)
        assert store.get_secret("CHAT_SETUP_X") == "tok2"
        store.delete_secret("CHAT_SETUP_X")
        assert store.get_secret("CHAT_SETUP_X") is None


def test_what_a_catalog_came_from_is_recorded(tmp_path: Path) -> None:
    store, repository, _ = _store(tmp_path)
    with repository:
        assert store.provenance("learn", "1.0.0") is None
        store.record_provenance(
            server_id="learn",
            catalog_version="1.0.0",
            origin="proposal",
            proposal_id="prop_1",
            proposal_digest="sha256:d",
            now=NOW,
        )
        assert store.provenance("learn", "1.0.0") == {
            "serverId": "learn",
            "catalogVersion": "1.0.0",
            "origin": "proposal",
            "proposalId": "prop_1",
            "proposalDigest": "sha256:d",
            "recordedAt": NOW,
        }


def test_sealed_text_and_chained_secrets() -> None:
    box = SecretBox(SecretBox.generate_key())
    sealed = box.seal("hello")
    assert "hello" not in sealed and box.open(sealed) == "hello"
    assert box.open("not a token") is None
    assert SecretBox(SecretBox.generate_key()).open(sealed) is None  # another key cannot read it

    secrets = ChainedSecrets({"A": "from env", "EMPTY": ""}, {"B": "stored", "EMPTY": "filled"}.get)
    assert secrets["A"] == "from env" and secrets["B"] == "stored" and secrets["EMPTY"] == "filled"
    assert secrets.get("C") is None and "A" in secrets and len(secrets) == 2
    assert sorted(secrets) == ["A", "EMPTY"]


def test_owner_codes_are_unambiguous_and_hashed_with_a_salt() -> None:
    code = owner.new_owner_code()
    assert len(code) == 14 and code.count("-") == 2
    assert not set(code) & set("01OIl")
    assert owner.new_salt() != owner.new_salt()
    assert owner.hash_code(code, "00" * 16) != owner.hash_code(code, "11" * 16)


def test_the_audit_chain_links_every_event_and_catches_tampering(tmp_path: Path) -> None:
    repository = SqliteControlPlaneRepository(tmp_path / "adapter.db")
    with repository:
        repository.initialize()
        assert repository.verify_admin_audit_chain().ok  # an empty trail is fine
        for number in range(4):
            repository.record_admin_audit(
                occurred_at=f"2026-10-05T10:00:0{number}Z",
                actor="t",
                action=f"a{number}",
                server_id=None,
                data={"n": number},
            )
        report = repository.verify_admin_audit_chain()
        assert report.ok and report.chained == 4 and report.head and report.unchained == 0

        raw = sqlite3.connect(tmp_path / "adapter.db")
        raw.execute("UPDATE admin_audit SET actor = 'someone else' WHERE event_id = 2")
        raw.commit()
        assert repository.verify_admin_audit_chain().bad_event_id == 2
        raw.execute("UPDATE admin_audit SET actor = 't' WHERE event_id = 2")
        raw.execute(
            "INSERT INTO admin_audit (occurred_at, actor, action, server_id, data_json) "
            "VALUES ('x','x','x',NULL,'{}')"
        )
        raw.commit()
        slipped = repository.verify_admin_audit_chain()
        assert not slipped.ok and "without a hash" in str(slipped.problem)
        raw.execute("DELETE FROM admin_audit WHERE action = 'x'")
        raw.execute("DELETE FROM admin_audit WHERE event_id = 4")  # the tail: not detectable alone
        raw.commit()
        assert repository.verify_admin_audit_chain().head != report.head  # but the head moved
        raw.close()


def test_events_from_before_the_chain_existed_are_counted_not_failed(tmp_path: Path) -> None:
    repository = SqliteControlPlaneRepository(tmp_path / "adapter.db")
    with repository:
        repository.initialize()
        raw = sqlite3.connect(tmp_path / "adapter.db")
        raw.execute(
            "INSERT INTO admin_audit (occurred_at, actor, action, server_id, data_json) "
            "VALUES ('t','old','x',NULL,'{}')"
        )
        raw.commit()
        raw.close()
        repository.record_admin_audit(
            occurred_at="t2", actor="new", action="y", server_id=None, data={}
        )
        report = repository.verify_admin_audit_chain()
        assert report.ok and report.unchained == 1 and report.chained == 1


def test_a_missing_discovery_snapshot_is_not_found(tmp_path: Path) -> None:
    repository = SqliteControlPlaneRepository(tmp_path / "adapter.db")
    with repository:
        repository.initialize()
        with pytest.raises(EntityNotFoundError):
            repository.get_discovery(42)
        assert repository.runtime_audit_for_request("nope") == ()


def test_a_noted_head_shows_what_came_after_it_and_notices_a_cut_tail(tmp_path: Path) -> None:
    repository = SqliteControlPlaneRepository(tmp_path / "adapter.db")
    with repository:
        repository.initialize()

        def write(number: int) -> None:
            repository.record_admin_audit(
                occurred_at=f"2026-10-05T10:00:0{number}Z",
                actor="t",
                action=f"a{number}",
                server_id=None,
                data={"n": number},
            )

        for number in range(3):
            write(number)
        noted = repository.verify_admin_audit_chain().head
        assert noted
        for number in range(3, 5):
            write(number)

        later = repository.verify_admin_audit_chain(noted)
        assert later.ok and later.rows_after_pin == 2
        assert repository.verify_admin_audit_chain(later.head).rows_after_pin == 0
        assert repository.verify_admin_audit_chain().rows_after_pin is None  # nothing was noted
        assert repository.verify_admin_audit_chain("sha256:" + "0" * 64).rows_after_pin is None

        # rows that existed when the head was noted are cut off: the chain still verifies,
        # but the noted head is no longer in it
        raw = sqlite3.connect(tmp_path / "adapter.db")
        raw.execute("DELETE FROM admin_audit WHERE event_id >= 3")
        raw.commit()
        raw.close()
        cut = repository.verify_admin_audit_chain(noted)
        assert cut.ok and cut.rows_after_pin is None
