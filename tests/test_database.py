import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from db.database import Database
from db.models import age_range
from tests.conftest import member, react


def test_mutual_like_creates_one_match_and_two_notifications(db):
    member(db, 1)
    member(db, 2)
    first = react(db, 1)
    assert not first["matched"]
    assert not db.matches(1)
    assert len(db.pending_notifications()) == 1
    assert react(db, 2, mode="likes")["matched"]
    assert [u["id"] for u in db.matches(1)] == [2]
    notices = db.pending_notifications()
    assert {(n["recipient"], n["kind"]) for n in notices} == {(1, "match"), (2, "match")}
    assert db.present(1) is None


def test_stale_forged_and_repeated_callbacks_do_not_create_likes(db):
    member(db, 1)
    member(db, 2)
    _, old = db.present(1)
    _, token = db.present(1)
    assert db.react(1, old, "like") is None
    assert db.react(2, token, "like") is None
    assert db.react(1, "made-up", "like") is None
    assert db.react(1, token, "like")
    assert db.react(1, token, "like") is None
    assert db.conn.execute("SELECT count(*) FROM reactions").fetchone()[0] == 1


@pytest.mark.parametrize("change", ["pause", "ban", "delete", "filter", "profile"])
def test_changed_target_invalidates_cards(db, change):
    member(db, 1)
    member(db, 2)
    _, token = db.present(1)
    if change == "pause":
        db.set_user(2, active=0)
    elif change == "ban":
        db.ban(2)
    elif change == "delete":
        db.delete_user(2)
    elif change == "filter":
        db.set_filters(2, 40, 60, False)
    else:
        member(db, 2, age=30)
    assert db.react(1, token, "like") is None


def test_both_preferences_and_city_filters_are_respected(db):
    member(db, 1, gender="male", seeking="female")
    member(db, 2, gender="female", seeking="female")
    assert db.present(1) is None
    member(db, 2, gender="female", seeking="male", city="Astana")
    assert db.present(1)
    db.set_filters(2, 18, 100, True)
    assert db.present(1) is None
    db.set_filters(2, 30, 40, False)
    assert db.present(1) is None
    db.set_filters(2, 18, 100, False)
    assert db.present(1)


def test_city_normalization(db):
    member(db, 1, city="  Алматы  ")
    member(db, 2, city="алматы")
    db.set_filters(1, 18, 100, True)
    assert db.present(1)


def test_feed_excludes_self_incomplete_and_skipped(db):
    member(db, 1)
    db.touch(2)
    assert db.present(1) is None
    member(db, 2)
    react(db, 1, "skip")
    assert db.present(1) is None
    with db.conn:
        db.conn.execute("UPDATE reactions SET created=?", (time.time() - 8 * 86400,))
    assert db.present(1)


def test_feed_has_varied_order(db):
    member(db, 1)
    for uid in range(2, 10):
        member(db, uid)
    assert len({db.present(1)[0]["id"] for _ in range(50)}) > 1


def test_incoming_likes_only_unanswered_compatible_profiles(db):
    member(db, 1)
    member(db, 2)
    assert db.present(2, "likes") is None
    react(db, 1)
    assert db.present(2, "likes")[0]["id"] == 1
    react(db, 2, "skip", "likes")
    assert db.present(2, "likes") is None
    assert not db.notification_valid(db.pending_notifications()[0])


def test_block_report_and_unblock(db):
    member(db, 1)
    member(db, 2)
    token = db.present(1)[1]
    assert db.react(1, token, "report", "spam")
    assert db.present(1) is None and db.present(2) is None
    assert db.reports()[0]["reason"] == "spam"
    assert not db.unblock(2, 1)
    assert db.unblock(1, 2)
    assert db.present(1)


def test_report_match_removes_pair_and_pending_messages(db):
    member(db, 1)
    member(db, 2)
    react(db, 1)
    react(db, 2)
    assert not db.report_match(1, 999, "abuse")
    assert db.report_match(1, 2, "abuse")
    assert not db.matches(1) and not db.matches(2)
    assert not db.pending_notifications()
    assert db.blocked_users(1)[0]["id"] == 2
    assert db.unblock(1, 2)
    assert db.present(1) and db.present(2)


@pytest.mark.parametrize("from_match", [False, True])
def test_fresh_report_reopens_previously_resolved_complaint(db, from_match):
    member(db, 1)
    member(db, 2)
    db.react(1, db.present(1)[1], "report", "spam")
    original = db.reports()[0]
    db.resolve(original["id"])
    assert not db.reports()
    db.unblock(1, 2)
    if from_match:
        react(db, 1)
        react(db, 2)
        assert db.report_match(1, 2, "abuse")
    else:
        db.react(1, db.present(1)[1], "report", "abuse")
    reports = db.reports()
    assert len(reports) == 1
    assert reports[0]["id"] == original["id"]
    assert reports[0]["reason"] == "abuse"
    assert reports[0]["created"] >= original["created"]
    assert reports[0]["resolved"] == 0
    assert db.blocked(1, 2)
    assert not db.is_match(1, 2)


def test_delete_cascades_and_anonymizes_reports(db):
    member(db, 1)
    member(db, 2)
    db.react(1, db.present(1)[1], "report", "spam")
    db.save_session(1, "name", {"name": "draft"})
    db.delete_user(1)
    assert db.user(1) is None
    assert db.session(1) is None
    assert db.reports()[0]["actor"] is None
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_banned_delete_retains_only_tombstone(db):
    member(db, 1)
    member(db, 2)
    db.react(2, db.present(2)[1], "report", "underage")
    db.ban(1)
    db.delete_user(1)
    user = db.touch(1, "returned")
    assert user["banned"] and not user["photo"] and not user["name"]
    assert user["username"] is None
    assert db.reports()[0]["target"] is None
    assert not db.blocked_users(2)
    with pytest.raises(ValueError):
        member(db, 1)


def test_restart_preserves_wizard_likes_and_notification_retries(tmp_path):
    path = tmp_path / "restart.sqlite3"
    db = Database(path)
    member(db, 1)
    member(db, 2)
    react(db, 1)
    db.save_session(2, "name", {"name": "draft", "nonce": "abc"})
    item = db.pending_notifications()[0]
    db.notification_retry(item["id"], 60)
    db.close()
    db = Database(path)
    assert db.session(2) == ("name", {"name": "draft", "nonce": "abc"})
    assert not db.pending_notifications()
    assert react(db, 2)["matched"]
    assert len(db.pending_notifications()) == 2
    db.close()


def test_concurrent_reciprocal_likes_are_atomic(tmp_path):
    path = tmp_path / "race.sqlite3"
    db = Database(path)
    member(db, 1)
    member(db, 2)
    tokens = {uid: db.present(uid)[1] for uid in (1, 2)}

    def action(uid):
        connection = Database(path)
        try:
            return connection.react(uid, tokens[uid], "like")
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(action, (1, 2)))
    assert sum(r["matched"] for r in result) == 1
    assert len(db.pending_notifications()) == 2
    assert db.stats()["matches"] == 1
    db.close()


def test_edit_cancel_preserves_live_profile_and_pause(db):
    member(db, 1)
    db.set_user(1, active=0)
    db.save_session(1, "name", {"name": "Changed"})
    db.clear_session(1)
    assert db.user(1)["name"] == "Person 1"
    member(db, 1, name="New name")
    assert not db.user(1)["active"]


@pytest.mark.parametrize("value", ["17-30", "40-20", "18-101", "oops", "18", "18-19-20"])
def test_invalid_age_ranges(value):
    with pytest.raises(ValueError):
        age_range(value)


@pytest.mark.parametrize("age", [17, 0, 101])
def test_registration_rejects_invalid_age(db, age):
    with pytest.raises(ValueError):
        member(db, 1, age=age)
    assert not db.user(1)["photo"]


def test_notification_suppressed_for_blocked_or_banned(db):
    member(db, 1)
    member(db, 2)
    react(db, 1)
    react(db, 2)
    item = db.pending_notifications()[0]
    db.ban(2)
    assert not db.notification_valid(item)


def test_foreign_keys_and_self_likes(db):
    member(db, 1)
    with pytest.raises(sqlite3.IntegrityError), db.conn:
        db.conn.execute("INSERT INTO reactions VALUES(1,1,'like',0)")
    with pytest.raises(sqlite3.IntegrityError), db.conn:
        db.conn.execute("INSERT INTO reactions VALUES(1,999,'like',0)")


def test_rate_limit(db):
    db.touch(1)
    assert db.rate_allowed(1)
    assert not db.rate_allowed(1)


def test_database_error_rolls_back_match_and_second_like(db):
    member(db, 1)
    member(db, 2)
    react(db, 1)
    token = db.present(2)[1]
    db.conn.execute(
        "CREATE TRIGGER fail_notification BEFORE INSERT ON outbox WHEN NEW.kind='match' "
        "BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.react(2, token, "like")
    assert not db.is_match(1, 2)
    assert db.view(2, token)
    assert db.conn.execute("SELECT count(*) FROM reactions").fetchone()[0] == 1
    db.conn.execute("DROP TRIGGER fail_notification")
    assert db.react(2, token, "like")["matched"]
