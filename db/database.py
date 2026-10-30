"""SQLite persistence. Reactions and match notifications commit together."""

import json
import secrets
import sqlite3
import time
from pathlib import Path

from db.models import MAX_AGE, MIN_AGE, normalize_interests, validate_profile
from matching import choose_candidate

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, username TEXT, lang TEXT NOT NULL DEFAULT 'en',
 name TEXT, age INTEGER CHECK(age BETWEEN 18 AND 100), gender TEXT,
 seeking TEXT NOT NULL DEFAULT 'any', city TEXT, city_key TEXT, bio TEXT, photo TEXT,
 min_age INTEGER NOT NULL DEFAULT 18, max_age INTEGER NOT NULL DEFAULT 100,
 local_only INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 0,
 banned INTEGER NOT NULL DEFAULT 0, reachable INTEGER NOT NULL DEFAULT 1,
 consent INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, last_request REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
 user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 step TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reactions (
 actor INTEGER REFERENCES users(id) ON DELETE CASCADE,
 target INTEGER REFERENCES users(id) ON DELETE CASCADE,
 kind TEXT NOT NULL CHECK(kind IN ('like','skip')), created REAL NOT NULL,
 PRIMARY KEY(actor,target), CHECK(actor != target)
);
CREATE INDEX IF NOT EXISTS incoming_likes ON reactions(target,kind);
CREATE TABLE IF NOT EXISTS matches (
 a INTEGER REFERENCES users(id) ON DELETE CASCADE,
 b INTEGER REFERENCES users(id) ON DELETE CASCADE, created REAL NOT NULL,
 PRIMARY KEY(a,b), CHECK(a < b)
);
CREATE TABLE IF NOT EXISTS blocks (
 actor INTEGER REFERENCES users(id) ON DELETE CASCADE,
 target INTEGER REFERENCES users(id) ON DELETE CASCADE,
 PRIMARY KEY(actor,target), CHECK(actor != target)
);
CREATE TABLE IF NOT EXISTS views (
 actor INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 target INTEGER REFERENCES users(id) ON DELETE CASCADE,
 token TEXT NOT NULL, mode TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 actor INTEGER REFERENCES users(id) ON DELETE SET NULL,
 target INTEGER REFERENCES users(id) ON DELETE SET NULL,
 reason TEXT NOT NULL, created REAL NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
 UNIQUE(actor,target)
);
CREATE TABLE IF NOT EXISTS outbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 recipient INTEGER REFERENCES users(id) ON DELETE CASCADE,
 other INTEGER REFERENCES users(id) ON DELETE CASCADE,
 kind TEXT NOT NULL CHECK(kind IN ('like','match')),
 attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0,
 delivered INTEGER NOT NULL DEFAULT 0, UNIQUE(recipient,other,kind)
);
CREATE INDEX IF NOT EXISTS pending_outbox ON outbox(delivered,due);
"""

IMPRESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS impressions (
 actor INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 target INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 last_shown REAL NOT NULL,
 PRIMARY KEY(actor,target), CHECK(actor != target)
)
"""
SCHEMA_VERSION = 2
EXPOSURE_COOLDOWN = 86400


class Database:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        try:
            # DDL and the version marker commit together. Running statements
            # individually avoids executescript's implicit pre-transaction commit.
            with self.conn:
                self.conn.execute("BEGIN IMMEDIATE")
                version = self.conn.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise RuntimeError("Database was created by a newer bot version")
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        self.conn.execute(statement)
                if version < 2:
                    columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(users)")}
                    for name, definition in (
                        ("interests", "TEXT NOT NULL DEFAULT '[]'"),
                        ("purpose", "TEXT NOT NULL DEFAULT 'any'"),
                        ("last_active", "REAL NOT NULL DEFAULT 0"),
                    ):
                        if name not in columns:
                            self.conn.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
                self.conn.execute(IMPRESSION_SCHEMA)
                self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except Exception:
            self.conn.close()
            raise

    def close(self):
        self.conn.close()

    def user(self, uid):
        row = self.conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return self._profile(row) if row else None

    @staticmethod
    def _profile(row):
        profile = dict(row)
        profile["interests"] = normalize_interests(profile.get("interests", "[]"))
        return profile

    def touch(self, uid, username=None, lang="en"):
        with self.conn:
            self.conn.execute(
                "INSERT INTO users(id,username,lang,created,last_active) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET username=CASE WHEN users.banned=0 THEN excluded.username ELSE users.username END,"
                "reachable=CASE WHEN users.banned=0 THEN 1 ELSE users.reachable END,"
                "last_active=CASE WHEN users.banned=0 THEN excluded.last_active ELSE users.last_active END",
                (uid, username, "ru" if lang.startswith("ru") else "en", time.time(), time.time()),
            )
        return self.user(uid)

    def set_user(self, uid, **values):
        allowed = {"lang", "active", "reachable", "min_age", "max_age", "local_only"}
        if not values or not set(values) <= allowed:
            raise ValueError("Invalid user fields")
        with self.conn:
            self.conn.execute(
                "UPDATE users SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                (*values.values(), uid),
            )
            self.conn.execute("DELETE FROM views WHERE actor=? OR target=?", (uid, uid))

    def rate_allowed(self, uid, interval=0.5):
        now = time.time()
        with self.conn:
            return bool(
                self.conn.execute(
                    "UPDATE users SET last_request=? WHERE id=? AND last_request<=?",
                    (now, uid, now - interval),
                ).rowcount
            )

    def session(self, uid):
        row = self.conn.execute("SELECT * FROM sessions WHERE user_id=?", (uid,)).fetchone()
        return (row["step"], json.loads(row["data"])) if row else None

    def save_session(self, uid, step, data):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?)", (uid, step, json.dumps(data)))
            self.conn.execute("DELETE FROM views WHERE actor=?", (uid,))

    def clear_session(self, uid):
        with self.conn:
            self.conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))

    def save_profile(self, uid, data):
        data = validate_profile(data)
        current = self.user(uid)
        if not current or current["banned"]:
            raise ValueError("Account unavailable")
        with self.conn:
            self.conn.execute(
                "UPDATE users SET name=?,age=?,gender=?,seeking=?,city=?,city_key=?,bio=?,"
                "photo=?,interests=?,purpose=?,consent=1,active=? WHERE id=?",
                (
                    data["name"],
                    int(data["age"]),
                    data["gender"],
                    data["seeking"],
                    data["city"],
                    data["city"].casefold(),
                    data["bio"],
                    data["photo"],
                    json.dumps(data["interests"]),
                    data["purpose"],
                    current["active"] if current["photo"] else 1,
                    uid,
                ),
            )
            self.conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            self.conn.execute("DELETE FROM views WHERE actor=? OR target=?", (uid, uid))

    def blocked(self, a, b):
        return bool(
            self.conn.execute(
                "SELECT 1 FROM blocks WHERE (actor=? AND target=?) OR (actor=? AND target=?)",
                (a, b, b, a),
            ).fetchone()
        )

    def eligible(self, a, b):
        if not a or not b or a["id"] == b["id"]:
            return False
        for person in (a, b):
            if person["banned"] or not person["active"] or not person["photo"] or not person["consent"]:
                return False
        if self.blocked(a["id"], b["id"]):
            return False
        for viewer, candidate in ((a, b), (b, a)):
            if not viewer["min_age"] <= candidate["age"] <= viewer["max_age"]:
                return False
            if viewer["seeking"] not in ("any", candidate["gender"]):
                return False
            if viewer["local_only"] and viewer["city_key"] != candidate["city_key"]:
                return False
        return True

    def candidates(self, uid, mode="feed", skip_days=7):
        if mode not in {"feed", "likes"}:
            raise ValueError("Invalid discovery mode")
        now = time.time()
        # These are hard mutual requirements; ranking can never override them.
        rows = self.conn.execute(
            """
            SELECT p.*,i.last_shown FROM users p JOIN users me ON me.id=?
            LEFT JOIN impressions i ON i.actor=me.id AND i.target=p.id
            WHERE p.id!=me.id AND p.active=1 AND p.banned=0 AND p.photo IS NOT NULL
              AND p.consent=1 AND me.active=1 AND me.banned=0 AND me.photo IS NOT NULL
              AND p.age BETWEEN me.min_age AND me.max_age
              AND me.age BETWEEN p.min_age AND p.max_age
              AND (me.seeking='any' OR me.seeking=p.gender)
              AND (p.seeking='any' OR p.seeking=me.gender)
              AND (me.local_only=0 OR me.city_key=p.city_key)
              AND (p.local_only=0 OR me.city_key=p.city_key)
              AND NOT EXISTS(SELECT 1 FROM blocks WHERE
                (actor=me.id AND target=p.id) OR (actor=p.id AND target=me.id))
              AND NOT EXISTS(SELECT 1 FROM reactions WHERE actor=me.id AND target=p.id
                AND (kind='like' OR created>?))
              AND NOT EXISTS(SELECT 1 FROM matches WHERE
                a=min(me.id,p.id) AND b=max(me.id,p.id))
              AND (?='feed' OR EXISTS(SELECT 1 FROM reactions WHERE
                actor=p.id AND target=me.id AND kind='like'))
        """,
            (uid, now - skip_days * 86400, mode),
        ).fetchall()
        if not rows:
            return None
        profiles = [self._profile(row) for row in rows]
        fresh = [p for p in profiles if p["last_shown"] is None or p["last_shown"] <= now - EXPOSURE_COOLDOWN]
        if fresh:
            pool = fresh
        else:
            # When the pool is exhausted, rotate through its oldest impressions.
            oldest = min(p["last_shown"] for p in profiles)
            pool = [p for p in profiles if p["last_shown"] <= oldest + 0.001]
        liked_profiles = [
            self._profile(row)
            for row in self.conn.execute(
                "SELECT p.* FROM reactions r JOIN users p ON p.id=r.target "
                "WHERE r.actor=? AND r.kind='like' AND p.photo IS NOT NULL AND p.banned=0 "
                "ORDER BY r.created DESC,r.target LIMIT 30",
                (uid,),
            )
        ]
        return choose_candidate(self.user(uid), pool, liked_profiles=liked_profiles, now=now)

    def present(self, uid, mode="feed", skip_days=7):
        row = self.candidates(uid, mode, skip_days)
        if not row:
            with self.conn:
                self.conn.execute("DELETE FROM views WHERE actor=?", (uid,))
            return None
        token = secrets.token_hex(8)
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO views VALUES(?,?,?,?)", (uid, row["id"], token, mode))
            self.conn.execute(
                "INSERT INTO impressions(actor,target,last_shown) VALUES(?,?,?) "
                "ON CONFLICT(actor,target) DO UPDATE SET last_shown=excluded.last_shown",
                (uid, row["id"], time.time()),
            )
        return dict(row), token

    def view(self, uid, token):
        row = self.conn.execute("SELECT * FROM views WHERE actor=? AND token=?", (uid, token)).fetchone()
        return dict(row) if row else None

    def react(self, uid, token, kind, reason="other"):
        if kind not in {"like", "skip", "block", "report"}:
            raise ValueError("Invalid reaction")
        # IMMEDIATE prevents two processes from both creating the same match.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            view = self.view(uid, token)
            if not view or not self.eligible(self.user(uid), self.user(view["target"])):
                self.conn.rollback()
                return None
            target = view["target"]
            self.conn.execute("DELETE FROM views WHERE actor=?", (uid,))
            matched = False
            if kind in {"block", "report"}:
                self._block(uid, target)
                if kind == "report":
                    self._report(uid, target, reason)
            else:
                self.conn.execute(
                    "INSERT INTO reactions VALUES(?,?,?,?) ON CONFLICT(actor,target) "
                    "DO UPDATE SET kind=excluded.kind,created=excluded.created",
                    (uid, target, kind, time.time()),
                )
                if kind == "like":
                    reciprocal = self.conn.execute(
                        "SELECT 1 FROM reactions WHERE actor=? AND target=? AND kind='like'",
                        (target, uid),
                    ).fetchone()
                    if reciprocal:
                        a, b = sorted((uid, target))
                        matched = bool(
                            self.conn.execute(
                                "INSERT OR IGNORE INTO matches VALUES(?,?,?)", (a, b, time.time())
                            ).rowcount
                        )
                        self.conn.execute(
                            "DELETE FROM outbox WHERE kind='like' AND "
                            "((recipient=? AND other=?) OR (recipient=? AND other=?))",
                            (a, b, b, a),
                        )
                        for recipient, other in ((a, b), (b, a)):
                            self.conn.execute(
                                "INSERT OR IGNORE INTO outbox(recipient,other,kind) VALUES(?,?,'match')",
                                (recipient, other),
                            )
                    else:
                        self.conn.execute(
                            "INSERT OR IGNORE INTO outbox(recipient,other,kind) VALUES(?,?,'like')", (target, uid)
                        )
            self.conn.commit()
            return {"matched": matched, "target": target, "mode": view["mode"]}
        except Exception:
            self.conn.rollback()
            raise

    def _block(self, uid, target):
        self.conn.execute("INSERT OR IGNORE INTO blocks VALUES(?,?)", (uid, target))
        a, b = sorted((uid, target))
        self.conn.execute("DELETE FROM matches WHERE a=? AND b=?", (a, b))
        self.conn.execute("DELETE FROM reactions WHERE (actor=? AND target=?) OR (actor=? AND target=?)", (a, b, b, a))
        self.conn.execute(
            "DELETE FROM outbox WHERE (recipient=? AND other=?) OR (recipient=? AND other=?)", (a, b, b, a)
        )
        self.conn.execute("DELETE FROM views WHERE (actor=? AND target=?) OR (actor=? AND target=?)", (a, b, b, a))

    def block_match(self, uid, target):
        if not self.is_match(uid, target):
            return False
        with self.conn:
            self._block(uid, target)
        return True

    def report_match(self, uid, target, reason):
        if reason not in {"spam", "underage", "abuse", "other"} or not self.is_match(uid, target):
            return False
        with self.conn:
            self._block(uid, target)
            self._report(uid, target, reason)
        return True

    def _report(self, uid, target, reason):
        # One record per pair avoids duplicate complaints, but a fresh report after
        # an earlier review must return to the moderation queue.
        self.conn.execute(
            "INSERT INTO reports(actor,target,reason,created) VALUES(?,?,?,?) "
            "ON CONFLICT(actor,target) DO UPDATE SET reason=excluded.reason,"
            "created=excluded.created,resolved=0",
            (uid, target, reason, time.time()),
        )

    def blocked_users(self, uid, offset=0):
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT u.id,u.name FROM blocks b JOIN users u ON u.id=b.target WHERE b.actor=? ORDER BY u.id LIMIT 6 OFFSET ?",
                (uid, offset),
            )
        ]

    def unblock(self, uid, target):
        with self.conn:
            return bool(self.conn.execute("DELETE FROM blocks WHERE actor=? AND target=?", (uid, target)).rowcount)

    def is_match(self, uid, target):
        a, b = sorted((uid, target))
        return bool(self.conn.execute("SELECT 1 FROM matches WHERE a=? AND b=?", (a, b)).fetchone())

    def matches(self, uid, offset=0):
        return [
            self._profile(row)
            for row in self.conn.execute(
                "SELECT u.* FROM matches m JOIN users u ON u.id=CASE WHEN m.a=? THEN m.b ELSE m.a END "
                "WHERE (m.a=? OR m.b=?) AND u.banned=0 ORDER BY m.created DESC,m.a,m.b LIMIT 6 OFFSET ?",
                (uid, uid, uid, offset),
            )
        ]

    def set_filters(self, uid, low, high, local_only):
        if not MIN_AGE <= low <= high <= MAX_AGE:
            raise ValueError("Invalid age filter")
        self.set_user(uid, min_age=low, max_age=high, local_only=int(local_only))

    def delete_user(self, uid):
        with self.conn:
            # Banned accounts retain a minimal tombstone to prevent /start ban evasion.
            if self.user(uid)["banned"]:
                self.conn.execute(
                    "UPDATE users SET name=NULL,age=NULL,gender=NULL,city=NULL,city_key=NULL,"
                    "bio=NULL,photo=NULL,username=NULL,consent=0,active=0,interests='[]',purpose='any',last_active=0 WHERE id=?",
                    (uid,),
                )
                for table, col in (("sessions", "user_id"), ("views", "actor")):
                    self.conn.execute(f"DELETE FROM {table} WHERE {col}=?", (uid,))
                self.conn.execute("DELETE FROM views WHERE target=?", (uid,))
                for table, first, second in (
                    ("reactions", "actor", "target"),
                    ("blocks", "actor", "target"),
                    ("matches", "a", "b"),
                    ("outbox", "recipient", "other"),
                    ("impressions", "actor", "target"),
                ):
                    self.conn.execute(f"DELETE FROM {table} WHERE {first}=? OR {second}=?", (uid, uid))
                self.conn.execute("UPDATE reports SET actor=NULL WHERE actor=?", (uid,))
                self.conn.execute("UPDATE reports SET target=NULL WHERE target=?", (uid,))
                self.conn.execute(
                    "UPDATE users SET seeking='any',min_age=18,max_age=100,local_only=0,lang='en',"
                    "created=0,last_request=0,reachable=0 WHERE id=?",
                    (uid,),
                )
            else:
                self.conn.execute("DELETE FROM users WHERE id=?", (uid,))

    def ban(self, uid, banned=True):
        if not self.user(uid):
            return False
        with self.conn:
            self.conn.execute("UPDATE users SET banned=?,active=0 WHERE id=?", (int(banned), uid))
            self.conn.execute("DELETE FROM views WHERE actor=? OR target=?", (uid, uid))
            self.conn.execute("DELETE FROM outbox WHERE recipient=? OR other=?", (uid, uid))
        return True

    def reports(self):
        return [dict(row) for row in self.conn.execute("SELECT * FROM reports WHERE resolved=0 ORDER BY id LIMIT 20")]

    def resolve(self, report_id):
        with self.conn:
            return self.conn.execute("UPDATE reports SET resolved=1 WHERE id=?", (report_id,)).rowcount

    def stats(self):
        return {
            "profiles": self.conn.execute("SELECT count(*) FROM users WHERE photo IS NOT NULL").fetchone()[0],
            "active": self.conn.execute("SELECT count(*) FROM users WHERE active=1 AND banned=0").fetchone()[0],
            "matches": self.conn.execute("SELECT count(*) FROM matches").fetchone()[0],
            "reports": self.conn.execute("SELECT count(*) FROM reports WHERE resolved=0").fetchone()[0],
        }

    def pending_notifications(self):
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM outbox WHERE delivered=0 AND due<=? ORDER BY id LIMIT 25", (time.time(),)
            )
        ]

    def notification_valid(self, item):
        a, b = self.user(item["recipient"]), self.user(item["other"])
        if not a or not b or a["banned"] or b["banned"] or not a["reachable"]:
            return False
        if not a["photo"] or not b["photo"] or self.blocked(a["id"], b["id"]):
            return False
        if item["kind"] == "match":
            return self.is_match(a["id"], b["id"])
        already_answered = self.conn.execute(
            "SELECT 1 FROM reactions WHERE actor=? AND target=?", (a["id"], b["id"])
        ).fetchone()
        return (
            not already_answered
            and self.eligible(a, b)
            and bool(
                self.conn.execute(
                    "SELECT 1 FROM reactions WHERE actor=? AND target=? AND kind='like'",
                    (b["id"], a["id"]),
                ).fetchone()
            )
        )

    def notification_done(self, oid):
        with self.conn:
            self.conn.execute("UPDATE outbox SET delivered=1 WHERE id=?", (oid,))

    def notification_retry(self, oid, delay):
        with self.conn:
            self.conn.execute("UPDATE outbox SET attempts=attempts+1,due=? WHERE id=?", (time.time() + delay, oid))
