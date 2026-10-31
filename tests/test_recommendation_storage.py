import json
import sqlite3

import pytest

import db.database as database_module
from db.database import SCHEMA, SCHEMA_VERSION, Database
from db.models import INTERESTS, normalize_interests
from tests.conftest import member


def legacy_database(path):
    """Create a genuine pre-recommendation database with its original columns."""
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA + "PRAGMA user_version=1;")
    assert "interests" not in {row[1] for row in connection.execute("PRAGMA table_info(users)")}
    for uid in (1, 2, 3):
        connection.execute(
            "INSERT INTO users(id,name,age,gender,city,city_key,bio,photo,consent,active,created) "
            "VALUES(?,?,25,'male','Almaty','almaty','Hello',?,1,1,100)",
            (uid, f"Person {uid}", f"photo{uid}"),
        )
    connection.execute("INSERT INTO reactions VALUES(1,2,'like',101)")
    connection.execute("INSERT INTO reactions VALUES(2,1,'like',102)")
    connection.execute("INSERT INTO matches VALUES(1,2,102)")
    connection.execute("INSERT INTO sessions VALUES(3,'bio',?)", (json.dumps({"name": "Draft", "nonce": "abc"}),))
    connection.execute("INSERT INTO outbox(recipient,other,kind) VALUES(1,2,'match')")
    connection.commit()
    connection.close()


def test_v1_migration_preserves_profiles_relationships_drafts_and_outbox(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    legacy_database(path)
    db = Database(path)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert db.user(1)["name"] == "Person 1"
    assert db.user(1)["interests"] == []
    assert db.user(1)["purpose"] == "any"
    assert db.user(1)["last_active"] == 0
    assert db.is_match(1, 2)
    assert db.conn.execute("SELECT count(*) FROM reactions").fetchone()[0] == 2
    assert db.session(3) == ("bio", {"name": "Draft", "nonce": "abc"})
    assert db.pending_notifications()[0]["kind"] == "match"
    assert db.candidates(3) is not None  # Old metadata is a valid cold start.
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    db.close()
    # Reopening a migrated database never resets either version or preferences.
    db = Database(path)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert db.is_match(1, 2)
    db.close()


def test_failed_migration_rolls_back_columns_and_version(tmp_path, monkeypatch):
    path = tmp_path / "failed-migration.sqlite3"
    legacy_database(path)
    connect = sqlite3.connect

    def failing_connection(*args, **kwargs):
        connection = connect(*args, **kwargs)
        alterations = 0

        def authorize(action, arg1, arg2, database, trigger):
            nonlocal alterations
            if action == sqlite3.SQLITE_ALTER_TABLE:
                alterations += 1
                if alterations == 2:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(database_module.sqlite3, "connect", failing_connection)
    with pytest.raises(sqlite3.DatabaseError):
        Database(path)
    with connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "interests" not in {row[1] for row in connection.execute("PRAGMA table_info(users)")}
        assert connection.execute("SELECT count(*) FROM users").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM matches").fetchone()[0] == 1


def test_newer_schema_is_rejected_without_resetting_version(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError, match="newer"):
        Database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []


def test_preferences_roundtrip_and_backwards_compatible_profile_input(db):
    member(db, 1, interests=[" MUSIC ", "books", "music"], purpose="friendship")
    user = db.user(1)
    assert user["interests"] == ["music", "books"]
    assert user["purpose"] == "friendship"
    assert user["last_active"] > 0
    member(db, 2)
    assert db.user(2)["interests"] == [] and db.user(2)["purpose"] == "any"


@pytest.mark.parametrize("value", ["not JSON", '"music"', ["unknown"], [None], list(INTERESTS[:6]), None])
def test_invalid_interests_are_rejected(value):
    with pytest.raises(ValueError):
        normalize_interests(value)


def test_empty_and_json_interests():
    assert normalize_interests([]) == []
    assert normalize_interests('["music", " MUSIC ", "books"]') == ["music", "books"]


def test_ranking_receives_only_eligible_profiles_and_bounded_like_history(db, monkeypatch):
    member(db, 1, gender="male", seeking="female", interests=["music"])
    member(db, 2, gender="female", seeking="male", interests=["music"])
    member(db, 3, gender="male")
    member(db, 4, gender="female", seeking="female")
    member(db, 5, gender="female", seeking="male", age=50)
    db.set_filters(1, 18, 40, False)
    member(db, 6, gender="female", seeking="male")
    db.ban(6)
    member(db, 7, gender="female", seeking="male")
    db.set_user(7, active=0)
    member(db, 8, gender="female", seeking="male")
    db.conn.execute("INSERT INTO blocks VALUES(8,1)")
    member(db, 9, gender="female", seeking="male")
    db.conn.execute("INSERT INTO reactions VALUES(1,9,'skip',?)", (database_module.time.time(),))
    member(db, 10, gender="female", seeking="male", city="Astana")
    db.set_filters(10, 18, 100, True)
    # Synthetic outgoing history is separate from fresh candidates.
    for uid in range(100, 135):
        member(db, uid, gender="female", seeking="male", interests=["books"])
        db.conn.execute("INSERT INTO reactions VALUES(1,?,'like',?)", (uid, uid))
    db.conn.commit()
    captured = {}

    def select(viewer, candidates, liked_profiles=(), now=None):
        captured.update(viewer=viewer, candidates=candidates, history=liked_profiles, now=now)
        return candidates[0]

    monkeypatch.setattr(database_module, "choose_candidate", select)
    assert db.candidates(1)["id"] == 2
    assert [p["id"] for p in captured["candidates"]] == [2]
    assert len(captured["history"]) == 30
    assert captured["history"][0]["id"] == 134
    assert captured["viewer"]["interests"] == ["music"]
    assert captured["now"] > 0


def test_discovery_rotates_unseen_then_oldest_impressions(db, monkeypatch):
    for uid in (1, 2, 3, 4):
        member(db, uid)
    monkeypatch.setattr(database_module, "choose_candidate", lambda viewer, candidates, **kwargs: candidates[0])
    clock = iter((1000, 1001, 1002, 1003, 1004, 1005, 1006, 1007))
    monkeypatch.setattr(database_module.time, "time", lambda: next(clock))
    seen = [db.present(1)[0]["id"] for _ in range(4)]
    assert seen[:3] == [2, 3, 4]
    assert seen[3] == 2
    assert db.conn.execute("SELECT count(*) FROM impressions WHERE actor=1").fetchone()[0] == 3


def test_old_impressions_can_reenter_ranked_pool(db, monkeypatch):
    for uid in (1, 2, 3):
        member(db, uid)
    db.conn.execute("INSERT INTO impressions VALUES(1,2,?)", (database_module.time.time() - 90000,))
    db.conn.execute("INSERT INTO impressions VALUES(1,3,?)", (database_module.time.time(),))
    db.conn.commit()
    monkeypatch.setattr(database_module, "choose_candidate", lambda viewer, candidates, **kwargs: candidates[0])
    assert db.present(1)[0]["id"] == 2


def test_incoming_like_mode_does_not_rank_non_likers(db, monkeypatch):
    for uid in (1, 2, 3):
        member(db, uid)
    db.conn.execute("INSERT INTO reactions VALUES(2,1,'like',100)")
    db.conn.commit()
    monkeypatch.setattr(database_module, "choose_candidate", lambda viewer, candidates, **kwargs: candidates[0])
    assert db.present(1, "likes")[0]["id"] == 2


@pytest.mark.parametrize("banned", [False, True])
def test_delete_removes_preference_data_and_exposure_history(db, banned):
    member(db, 1, interests=["books"], purpose="relationship")
    member(db, 2)
    db.present(1)
    db.present(2)
    if banned:
        db.ban(1)
    db.delete_user(1)
    user = db.user(1)
    if banned:
        assert user["banned"] and user["interests"] == [] and user["purpose"] == "any" and user["last_active"] == 0
        db.touch(1, "returns")
        assert db.user(1)["last_active"] == 0
    else:
        assert user is None
    assert db.conn.execute("SELECT count(*) FROM impressions WHERE actor=1 OR target=1").fetchone()[0] == 0


def test_impression_and_card_are_atomic(db):
    member(db, 1)
    member(db, 2)
    db.conn.execute(
        "CREATE TRIGGER fail_impression BEFORE INSERT ON impressions "
        "BEGIN SELECT RAISE(ABORT,'simulated disk error'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.present(1)
    assert db.conn.execute("SELECT count(*) FROM views").fetchone()[0] == 0
    assert db.conn.execute("SELECT count(*) FROM impressions").fetchone()[0] == 0
