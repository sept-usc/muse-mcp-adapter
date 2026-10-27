# pyright: basic
"""The owner's storage: passcode, held actions, approvals that are spent once, known recipients."""

import sqlite3
from pathlib import Path

import pytest

from muse_mcp_adapter.domain.errors import ConflictError
from muse_mcp_adapter.persistence.owner_store import (
    MAX_FAILED_ATTEMPTS,
    MIN_PASSCODE_LENGTH,
    OwnerStore,
)
from muse_mcp_adapter.persistence.sqlite_repository import SqliteControlPlaneRepository

T0 = "2026-10-26T12:00:00Z"
T1 = "2026-10-26T12:05:00Z"
LATER = "2026-10-26T13:00:00Z"


@pytest.fixture
def store(tmp_path: Path):
    with SqliteControlPlaneRepository(tmp_path / "a.db") as repository:
        repository.initialize()
        yield OwnerStore(repository), tmp_path / "a.db"


def make(store: OwnerStore, digest: str = "d1", now: str = T0, ttl: int = 900, **kwargs):
    return store.create_request(
        connection_id=kwargs.get("connection_id", "c1"),
        server_id="s",
        public_operation_id="mail_send",
        classification="write",
        content_digest=digest,
        arguments={"to": "a@b.test"},
        reasons=["why"],
        recipients=["a@b.test"],
        now=now,
        ttl_seconds=ttl,
    )


def test_the_passcode_is_checked_and_never_stored_in_the_clear(store) -> None:
    owner, database = store
    assert not owner.passcode_is_set()
    owner.set_passcode("correct horse", T0)
    assert owner.passcode_is_set()
    assert owner.check_passcode("correct horse", T0)
    assert not owner.check_passcode("wrong horse", T0)
    raw = sqlite3.connect(database).execute("SELECT * FROM owner_secret").fetchall()
    assert "correct horse" not in str(raw)


def test_a_short_passcode_is_refused(store) -> None:
    owner, _ = store
    with pytest.raises(ValueError, match=str(MIN_PASSCODE_LENGTH)):
        owner.set_passcode("short", T0)
    assert not owner.passcode_is_set()


def test_changing_the_passcode_replaces_it_and_clears_the_lock(store) -> None:
    owner, _ = store
    owner.set_passcode("first passcode", T0)
    for _ in range(MAX_FAILED_ATTEMPTS):
        owner.check_passcode("nope nope nope", T0)
    assert owner.locked(T0)
    owner.set_passcode("second passcode", T0)
    assert not owner.locked(T0)
    assert owner.check_passcode("second passcode", T0)
    assert not owner.check_passcode("first passcode", T0)


def test_wrong_guesses_lock_it_and_the_right_one_does_not_get_in_while_locked(store) -> None:
    owner, _ = store
    owner.set_passcode("correct horse", T0)
    for _ in range(MAX_FAILED_ATTEMPTS):
        assert not owner.check_passcode("guess guess", T0)
    assert owner.locked(T0)
    assert not owner.check_passcode("correct horse", T0)  # locked: even the right one fails
    assert not owner.locked(LATER)  # the lock ends by itself
    assert owner.check_passcode("correct horse", LATER)


def test_a_right_passcode_resets_the_count(store) -> None:
    owner, _ = store
    owner.set_passcode("correct horse", T0)
    for _ in range(MAX_FAILED_ATTEMPTS - 1):
        owner.check_passcode("guess guess", T0)
    assert owner.check_passcode("correct horse", T0)
    for _ in range(MAX_FAILED_ATTEMPTS - 1):
        owner.check_passcode("guess guess", T0)
    assert not owner.locked(T0)


def test_without_a_passcode_nothing_is_accepted(store) -> None:
    owner, _ = store
    assert not owner.check_passcode("anything at all", T0)


def test_only_the_hash_of_a_link_token_is_stored(store) -> None:
    owner, database = store
    request, token = make(owner)
    assert owner.by_token(token).request_id == request.request_id
    assert owner.by_token(token + "x") is None
    dump = str(sqlite3.connect(database).execute("SELECT * FROM approval_requests").fetchall())
    assert token not in dump


def test_a_request_keeps_the_exact_arguments_and_reasons(store) -> None:
    owner, _ = store
    request, _ = make(owner)
    assert request.arguments == {"to": "a@b.test"}
    assert request.reasons == ("why",) and request.recipients == ("a@b.test",)
    assert request.status == "pending" and request.expires_at == "2026-10-26T12:15:00Z"


def test_a_decision_is_made_once(store) -> None:
    owner, _ = store
    request, _ = make(owner)
    decided = owner.decide(request.request_id, approve=True, by="owner-page", now=T1)
    assert decided.status == "approved" and decided.decided_by == "owner-page"
    with pytest.raises(ConflictError):
        owner.decide(request.request_id, approve=False, by="owner-page", now=T1)


def test_an_expired_request_cannot_be_decided(store) -> None:
    owner, _ = store
    request, _ = make(owner, ttl=60)
    assert request.expired(T1)
    with pytest.raises(ConflictError):
        owner.decide(request.request_id, approve=True, by="x", now=T1)


def test_an_approval_is_spent_once(store) -> None:
    owner, _ = store
    request, _ = make(owner)
    with pytest.raises(ConflictError):
        owner.use(request.request_id, "op-1", T1)  # not approved yet
    owner.decide(request.request_id, approve=True, by="x", now=T1)
    owner.use(request.request_id, "op-1", T1)
    with pytest.raises(ConflictError):
        owner.use(request.request_id, "op-2", T1)
    assert owner.get(request.request_id).status == "used"
    assert owner.get(request.request_id).operation_id == "op-1"


def test_a_declined_request_cannot_be_used(store) -> None:
    owner, _ = store
    request, _ = make(owner)
    owner.decide(request.request_id, approve=False, by="x", now=T1)
    with pytest.raises(ConflictError):
        owner.use(request.request_id, "op-1", T1)


def test_the_newest_request_for_an_action_is_the_one_that_counts(store) -> None:
    owner, _ = store
    first, _ = make(owner, now=T0)
    owner.decide(first.request_id, approve=True, by="x", now=T0)
    owner.use(first.request_id, "op-1", T0)
    second, _ = make(owner, now=T1)
    assert owner.latest_for("c1", "d1").request_id == second.request_id
    assert owner.latest_for("c1", "other") is None
    assert owner.latest_for("c2", "d1") is None


def test_pending_requests_are_counted_per_connection_and_expire(store) -> None:
    owner, _ = store
    make(owner, digest="a", ttl=60)
    make(owner, digest="b")
    make(owner, digest="c", connection_id="c2")
    assert owner.count_pending("c1", T0) == 2
    assert owner.count_pending("c1", T1) == 1  # the 60-second one has expired
    assert owner.count_pending("c2", T0) == 1


def test_a_reissued_token_replaces_the_old_one(store) -> None:
    owner, _ = store
    request, old = make(owner)
    new = owner.reissue_token(request.request_id)
    assert owner.by_token(old) is None
    assert owner.by_token(new).request_id == request.request_id


def test_a_reissue_does_nothing_once_decided(store) -> None:
    owner, _ = store
    request, old = make(owner)
    owner.decide(request.request_id, approve=True, by="x", now=T0)
    owner.reissue_token(request.request_id)
    assert owner.by_token(old) is not None  # unchanged: nothing was pending to reissue


def test_known_recipients_are_per_connection_and_case_insensitive(store) -> None:
    owner, _ = store
    owner.remember_recipients("c1", ["Dana@Family.test", "dana@family.test"], T0)
    assert owner.known_recipients("c1") == {"dana@family.test"}
    assert owner.known_recipients("c2") == set()


def test_listing_filters_by_status(store) -> None:
    owner, _ = store
    a, _ = make(owner, digest="a")
    b, _ = make(owner, digest="b")
    owner.decide(a.request_id, approve=True, by="x", now=T0)
    assert [r.request_id for r in owner.list_requests("pending")] == [b.request_id]
    assert len(owner.list_requests()) == 2
