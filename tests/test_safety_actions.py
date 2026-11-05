"""Moderation is authorized by a shown card, independent of later eligibility."""

import sqlite3

import pytest

import db.database as database_module
from db.database import Database
from tests.conftest import member


def change_target(db, change):
    if change == "profile":
        member(db, 2, name="Changed", age=30)
    elif change == "filters":
        db.set_filters(2, 40, 60, True)
    elif change == "language":
        db.set_user(2, lang="ru")
    elif change == "ban":
        db.ban(2)
    else:
        db.set_user(2, active=0)


@pytest.mark.parametrize("action", ["block", "report"])
@pytest.mark.parametrize("change", ["profile", "filters", "language", "pause", "ban"])
def test_shown_card_can_still_block_or_report_changed_target(db, action, change):
    member(db, 1)
    member(db, 2)
    token = db.present(1)[1]
    change_target(db, change)
    assert db.react(1, token, "like") is None
    assert db.react(1, token, "skip") is None
    assert db.view(1, token), "The report reason prompt still needs its server-issued capability"
    result = db.react(1, token, action, "underage")
    assert result == {"matched": False, "target": 2, "mode": "feed"}
    assert db.blocked(1, 2)
    assert db.react(1, token, action, "underage") is None
    assert not db.view(1, token)
    if action == "report":
        assert db.reports()[0]["reason"] == "underage"
        assert db.reports()[0]["target"] == 2
    else:
        assert not db.reports()


def test_repeated_target_changes_do_not_remove_safety_capability(db):
    member(db, 1)
    member(db, 2)
    token = db.present(1)[1]
    for change in ("language", "profile", "filters", "pause", "ban"):
        change_target(db, change)
    assert db.react(1, token, "report", "spam")["mode"] == "feed"


def test_safety_capability_preserves_likes_feed_mode(db):
    member(db, 1)
    member(db, 2)
    assert db.react(2, db.present(2)[1], "like")
    token = db.present(1, "likes")[1]
    db.set_user(2, active=0)
    assert db.react(1, token, "report", "abuse")["mode"] == "likes"
    assert not db.pending_notifications()


@pytest.mark.parametrize("action", ["block", "report"])
def test_moderation_rejects_forged_wrong_owner_and_superseded_tokens(db, action):
    member(db, 1)
    member(db, 2)
    old_token = db.present(1)[1]
    new_token = db.present(1)[1]
    db.set_user(2, lang="ru")
    assert db.react(1, "2", action) is None
    assert db.react(2, new_token, action) is None
    assert db.react(1, old_token, action) is None
    assert not db.blocked(1, 2)
    assert db.react(1, new_token, action)


@pytest.mark.parametrize("deleted", [1, 2])
def test_delete_and_reregister_cannot_reuse_moderation_capability(db, deleted):
    member(db, 1)
    member(db, 2)
    token = db.present(1)[1]
    db.set_user(2, active=0)
    db.delete_user(deleted)
    member(db, deleted)
    assert db.react(1, token, "report", "spam") is None
    assert not db.reports()
    assert not db.blocked(1, 2)
    assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()


def test_banned_deleted_tombstone_drops_moderation_capability(db):
    member(db, 1)
    member(db, 2)
    token = db.present(1)[1]
    db.ban(2)
    db.delete_user(2)
    assert not db.view(1, token)
    assert db.react(1, token, "report", "underage") is None


def test_banned_actor_cannot_use_existing_capability(db):
    member(db, 1)
    member(db, 2)
    token = db.present(1)[1]
    db.ban(1)
    assert db.react(1, token, "report", "spam") is None
    assert not db.reports()


def test_invalid_report_reason_does_not_consume_capability(db):
    member(db, 1)
    member(db, 2)
    token = db.present(1)[1]
    db.set_user(2, active=0)
    with pytest.raises(ValueError, match="reason"):
        db.react(1, token, "report", "invented")
    assert db.view(1, token)
    assert db.react(1, token, "report", "spam")


def test_failed_report_rolls_back_block_and_keeps_capability(db):
    member(db, 1)
    member(db, 2)
    token = db.present(1)[1]
    db.set_user(2, active=0)
    db.conn.execute(
        "CREATE TRIGGER fail_report BEFORE INSERT ON reports BEGIN SELECT RAISE(ABORT,'simulated write failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.react(1, token, "report", "abuse")
    assert not db.blocked(1, 2)
    assert db.view(1, token)
    db.conn.execute("DROP TRIGGER fail_report")
    assert db.react(1, token, "report", "abuse")


def test_safety_capability_survives_database_restart(tmp_path):
    path = tmp_path / "restart.sqlite3"
    db = Database(path)
    try:
        member(db, 1)
        member(db, 2)
        token = db.present(1)[1]
        db.set_user(2, active=0)
    finally:
        db.close()
    db = Database(path)
    try:
        assert db.react(1, token, "report", "spam")
        assert db.reports()[0]["target"] == 2
    finally:
        db.close()


def test_rate_limit_uses_monotonic_time_after_wall_clock_rollback(db, monkeypatch):
    db.touch(1)
    with db.conn:
        db.conn.execute("UPDATE users SET last_request=9999999999 WHERE id=1")
    clock = {"monotonic": 100.0, "wall": 10000.0}
    monkeypatch.setattr(database_module.time, "monotonic", lambda: clock["monotonic"])
    monkeypatch.setattr(database_module.time, "time", lambda: clock["wall"])
    assert db.rate_allowed(1)
    clock.update(monotonic=100.25, wall=6400.0)
    assert not db.rate_allowed(1)
    clock["monotonic"] = 100.5
    assert db.rate_allowed(1)


def test_rate_limiter_does_not_write_database_and_is_per_user(db):
    member(db, 1)
    member(db, 2)
    before = db.conn.total_changes
    assert db.rate_allowed(1)
    assert not db.rate_allowed(1)
    assert db.rate_allowed(2)
    assert db.conn.total_changes == before


def test_rate_limiter_removes_deleted_accounts_and_clears_on_close(db):
    member(db, 1)
    assert db.rate_allowed(1)
    db.delete_user(1)
    assert 1 not in db._request_deadlines
    assert not db.rate_allowed(1)
    member(db, 1)
    assert db.rate_allowed(1)
    db.close()
    assert not db._request_deadlines


def test_rate_limiter_registry_is_bounded_and_expired_entries_are_removed(db, monkeypatch):
    monkeypatch.setattr(database_module, "MAX_RATE_ENTRIES", 4)
    clock = {"now": 100.0}
    monkeypatch.setattr(database_module.time, "monotonic", lambda: clock["now"])
    for uid in range(1, 8):
        db.touch(uid)
        assert db.rate_allowed(uid)
    assert len(db._request_deadlines) == 4
    clock["now"] = 101.0
    assert db.rate_allowed(7)
    assert list(db._request_deadlines) == [7]
