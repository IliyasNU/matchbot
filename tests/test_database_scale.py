import itertools
import json
import sqlite3
import time

import pytest

import db.database as database_module
from db.database import IMPRESSION_SCHEMA, RANKING_SAMPLE_SIZE, SCHEMA, SCHEMA_VERSION, Database


def seed_many(db, count=1001):
    rows = [
        (uid, f"Synthetic {uid}", f"synthetic_{uid}", "bio " * 100, f"photo-{uid}", json.dumps(["books", "travel"]))
        for uid in range(1, count + 1)
    ]
    with db.conn:
        db.conn.executemany(
            "INSERT INTO users(id,name,username,bio,photo,interests,age,gender,seeking,city,city_key,consent,active,created,last_active) "
            "VALUES(?,?,?,?,?,?,28,'male','any','Almaty','almaty',1,1,1,1)",
            rows,
        )


def test_large_pool_ranks_only_compact_bounded_metadata_and_loads_one_selected_profile(db, monkeypatch):
    seed_many(db)
    original = db._profile
    full_reads = []

    def record_profile(row):
        if "bio" in row.keys():
            full_reads.append(row["id"])
        return original(row)

    captured = {}

    def select(viewer, candidates, **kwargs):
        captured.update(viewer=viewer, pool=candidates)
        return candidates[-1]

    monkeypatch.setattr(db, "_profile", record_profile)
    monkeypatch.setattr(database_module, "choose_candidate", select)
    selected = db.candidates(1)
    pool = captured["pool"]
    assert len(pool) == RANKING_SAMPLE_SIZE == 256
    assert all({"id", "age", "city_key", "interests", "purpose", "last_active"} <= p.keys() for p in pool)
    assert all(not {"bio", "photo", "username", "name"} & p.keys() for p in pool)
    assert full_reads == [1, selected["id"]]
    assert selected["bio"] == "bio " * 100
    assert selected["username"] == f"synthetic_{selected['id']}"
    assert selected["interests"] == ["books", "travel"]


def test_uniform_sql_sampling_does_not_limit_eligibility_to_low_ids(db, monkeypatch):
    seed_many(db)
    counter = itertools.count()
    # A deterministic reverse order replaces SQLite's random source in this test.
    db.conn.create_function("random", 0, lambda: -next(counter))
    captured = []

    def select(viewer, candidates, **kwargs):
        captured.extend(p["id"] for p in candidates)
        return candidates[-1]

    monkeypatch.setattr(database_module, "choose_candidate", select)
    assert db.candidates(1)["id"] > 256
    assert len(captured) == 256
    assert max(captured) == 1001
    assert min(captured) > 256


def test_filtering_precedes_shortlisting_and_changes_are_not_cached(db):
    seed_many(db)
    with db.conn:
        db.conn.execute("UPDATE users SET seeking='female' WHERE id=1")
        db.conn.execute("UPDATE users SET gender='female' WHERE id=1001")
    assert db.candidates(1)["id"] == 1001
    db.set_user(1001, active=0)
    assert db.candidates(1) is None
    db.set_user(1001, active=1)
    assert db.candidates(1)["id"] == 1001
    with db.conn:
        db.conn.execute("INSERT INTO blocks VALUES(1001,1)")
    assert db.candidates(1) is None


def test_fresh_candidate_beyond_shortlist_wins_and_oldest_bucket_still_rotates(db, monkeypatch):
    seed_many(db)
    monkeypatch.setattr(database_module.time, "time", lambda: 1100)
    with db.conn:
        db.conn.executemany("INSERT INTO impressions VALUES(1,?,1000)", [(uid,) for uid in range(2, 1001)])
    # One unshown candidate must not disappear behind the bounded sample.
    assert db.candidates(1)["id"] == 1001
    with db.conn:
        db.conn.execute("INSERT INTO impressions VALUES(1,1001,900)")
    assert db.present(1)[0]["id"] == 1001
    assert db.candidates(1)["id"] != 1001


def test_incoming_like_mode_uses_target_index_and_only_returns_likers(db):
    seed_many(db)
    with db.conn:
        db.conn.execute("INSERT INTO reactions VALUES(1001,1,'like',1)")
    assert db.candidates(1, "likes")["id"] == 1001
    source, params = db._discovery_query(db.user(1), "likes", time.time(), 7)
    plan = db.conn.execute("EXPLAIN QUERY PLAN SELECT p.id " + source, params).fetchall()
    details = [row["detail"] for row in plan]
    incoming_search = next(detail for detail in details if "SEARCH incoming" in detail)
    assert "target=?" in incoming_search and "kind=?" in incoming_search
    assert "incoming_like_candidates" in incoming_search
    assert not any("SCAN p" in detail for detail in details)


def test_selective_discovery_uses_partial_profile_index(db):
    seed_many(db)
    with db.conn:
        db.conn.execute("UPDATE users SET gender='female' WHERE id%2=0")
        db.conn.execute("UPDATE users SET city='Astana',city_key='astana' WHERE id%3=0")
        db.conn.execute("UPDATE users SET seeking='female',local_only=1 WHERE id=1")
    source, params = db._discovery_query(db.user(1), "feed", time.time(), 7)
    plan = db.conn.execute("EXPLAIN QUERY PLAN SELECT p.id " + source, params).fetchall()
    assert any("SEARCH p USING INDEX discover_city_gender_age" in row["detail"] for row in plan)


def make_v2(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        SCHEMA
        + "ALTER TABLE users ADD COLUMN interests TEXT NOT NULL DEFAULT '[]';"
        + "ALTER TABLE users ADD COLUMN purpose TEXT NOT NULL DEFAULT 'any';"
        + "ALTER TABLE users ADD COLUMN last_active REAL NOT NULL DEFAULT 0;"
        + IMPRESSION_SCHEMA
        + ";PRAGMA user_version=2;"
    )
    for uid in (1, 2):
        connection.execute(
            "INSERT INTO users(id,name,age,gender,seeking,city,city_key,bio,photo,consent,active,created,interests,purpose) "
            "VALUES(?,?,28,'male','any','Almaty','almaty','Hello',?,1,1,1,?, 'friendship')",
            (uid, f"Member {uid}", f"photo-{uid}", '["books"]'),
        )
    connection.execute("INSERT INTO reactions VALUES(1,2,'like',1)")
    connection.execute("INSERT INTO reactions VALUES(2,1,'like',1)")
    connection.execute("INSERT INTO matches VALUES(1,2,1)")
    connection.execute("INSERT INTO sessions VALUES(1,'name',?)", (json.dumps({"name": "draft"}),))
    connection.execute("INSERT INTO outbox(recipient,other,kind) VALUES(1,2,'match')")
    connection.execute("INSERT INTO impressions VALUES(1,2,1)")
    connection.commit()
    connection.close()


def test_v2_to_v3_migration_adds_indexes_without_losing_state(tmp_path):
    path = tmp_path / "v2.sqlite3"
    make_v2(path)
    db = Database(path)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
    assert db.user(1)["interests"] == ["books"]
    assert db.user(1)["purpose"] == "friendship"
    assert db.is_match(1, 2)
    assert db.session(1) == ("name", {"name": "draft"})
    assert len(db.pending_notifications()) == 1
    assert db.conn.execute("SELECT count(*) FROM reactions").fetchone()[0] == 2
    assert db.conn.execute("SELECT count(*) FROM impressions").fetchone()[0] == 1
    indexes = {row["name"] for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"discover_age", "incoming_like_candidates", "recent_outgoing_likes", "impressions_by_target"} <= indexes
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    db.close()


def test_failed_v3_index_migration_rolls_back_and_keeps_v2(tmp_path, monkeypatch):
    path = tmp_path / "v2-failure.sqlite3"
    make_v2(path)
    connect = sqlite3.connect

    def failing_connection(*args, **kwargs):
        connection = connect(*args, **kwargs)

        def authorize(action, arg1, arg2, database, trigger):
            if action == sqlite3.SQLITE_CREATE_INDEX and arg1 == "discover_gender_age":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(database_module.sqlite3, "connect", failing_connection)
    with pytest.raises(sqlite3.DatabaseError):
        Database(path)
    with connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM matches").fetchone()[0] == 1
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='discover_age'").fetchall()


def test_scaled_relationship_cascades_remove_only_deleted_users_data(db):
    seed_many(db)
    with db.conn:
        db.conn.executemany("INSERT INTO reactions VALUES(?,1,'like',1)", [(uid,) for uid in range(2, 1002)])
        db.conn.executemany("INSERT INTO impressions VALUES(?,1,1)", [(uid,) for uid in range(2, 1002)])
        db.conn.executemany("INSERT INTO views VALUES(?,1,?,'feed')", [(uid, f"token-{uid}") for uid in range(2, 1002)])
    db.delete_user(1)
    assert db.conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1000
    for table in ("reactions", "impressions", "views"):
        assert db.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
