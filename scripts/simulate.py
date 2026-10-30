"""Offline population test. All synthetic users live in an auto-deleted temporary DB.

Run: python scripts/simulate.py --users 1000 --requests 3000 --report report.json
No .env, bot token, real Telegram accounts or external network are used.
"""

import argparse
import asyncio
import json
import random
import statistics
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import MatchBot  # noqa: E402
from config import Settings  # noqa: E402
from db.database import Database  # noqa: E402
from db.models import INTERESTS  # noqa: E402


def timings(values):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}
    return {
        "count": len(values),
        "mean_ms": round(statistics.mean(values), 3),
        **{f"p{p}_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * p / 100))], 3) for p in (50, 95, 99)},
        "max_ms": round(ordered[-1], 3),
    }


def populate(db, count, rng):
    people = {}
    for uid in range(1, count + 1):
        broad = uid <= 100
        person = dict(
            id=uid,
            name=f"Synthetic {uid:04}",
            age=28 if broad else rng.randint(18, 65),
            gender=rng.choice(("male", "female", "other")),
            seeking="any" if broad else rng.choice(("any", "any", "male", "female", "other")),
            city="Almaty" if broad else rng.choice(("Almaty", "Astana", "Shymkent", "Kyzylorda", "Karaganda")),
            interests=rng.sample(INTERESTS, rng.randint(1, 5)),
            purpose=rng.choice(("any", "relationship", "friendship", "chat")),
            bio="Fictional offline test profile",
            photo=f"simulation-photo-{uid}",
            consent=1,
            active=1,
            banned=0,
            min_age=18,
            max_age=100,
            local_only=0,
        )
        if not broad:
            person.update(min_age=max(18, person["age"] - 8), max_age=min(100, person["age"] + 12))
            person["local_only"] = int(rng.random() < 0.25)
        db.touch(uid, f"synthetic_{uid:04}", "ru" if uid % 2 else "en")
        if not broad and uid % 31 == 0:
            person.update(photo=None, consent=0, active=0)
        else:
            db.save_profile(uid, person)
        db.set_filters(uid, person["min_age"], person["max_age"], person["local_only"])
        if not broad and uid % 29 == 0:
            person["active"] = 0
            db.set_user(uid, active=0)
        if not broad and uid % 37 == 0:
            person.update(banned=1, active=0)
            db.ban(uid)
        people[uid] = person
    return people


def eligible_ids(uid, people, blocks, likes, skips, pairs):
    """Independent oracle over fixture data; deliberately does not use DB.eligible."""
    me = people[uid]
    if me["banned"] or not me["active"] or not me["photo"] or not me["consent"]:
        return set()
    result = set()
    for target, other in people.items():
        if target == uid or other["banned"] or not other["active"] or not other["photo"] or not other["consent"]:
            continue
        if (uid, target) in blocks or (target, uid) in blocks:
            continue
        if (uid, target) in likes or (uid, target) in skips or tuple(sorted((uid, target))) in pairs:
            continue
        if not me["min_age"] <= other["age"] <= me["max_age"] or not other["min_age"] <= me["age"] <= other["max_age"]:
            continue
        if me["seeking"] not in ("any", other["gender"]) or other["seeking"] not in ("any", me["gender"]):
            continue
        if (me["local_only"] or other["local_only"]) and me["city"].casefold() != other["city"].casefold():
            continue
        result.add(target)
    return result


async def simulate(args):
    started = time.perf_counter()
    rng = random.Random(args.seed)
    random.seed(args.seed)
    sent = []
    report = {"users": args.users, "seed": args.seed, "telegram_transport": "in-memory fake; no external requests"}
    with tempfile.TemporaryDirectory(prefix="matchbot-simulation-", dir=args.temp_root) as temporary:
        path = Path(temporary) / "population.sqlite3"
        db = Database(path)
        try:
            people = populate(db, args.users, rng)
            assert db.conn.execute("SELECT count(*) FROM users").fetchone()[0] == args.users
            blocks = (
                {tuple(rng.sample(range(101, args.users + 1), 2)) for _ in range(250)} if args.users > 102 else set()
            )
            with db.conn:
                db.conn.executemany("INSERT OR IGNORE INTO blocks VALUES(?,?)", sorted(blocks))
            pairs, likes, skips, shown = set(), set(), set(), set()
            active = [uid for uid, p in people.items() if p["active"] and not p["banned"] and p["photo"]]
            report["population"] = {
                "active_complete": len(active),
                "paused": sum(not p["active"] and not p["banned"] and bool(p["photo"]) for p in people.values()),
                "banned": sum(p["banned"] for p in people.values()),
                "incomplete": sum(not p["photo"] for p in people.values()),
                "directed_blocks": len(blocks),
            }

            async def fake_send(chat_id, text, **kwargs):
                sent.append((chat_id, text))

            def service(database):
                bot = MatchBot(database, Settings("123456:simulation-only", path))
                bot.application = SimpleNamespace(bot=SimpleNamespace(send_message=fake_send))
                return bot

            bot = service(db)
            print(f"Created {args.users} synthetic profiles; testing mutual likes...", flush=True)
            attempts = 0
            while len(pairs) < args.matches:
                attempts += 1
                assert attempts <= args.matches * 10 + 100, "Could not produce enough mutual matches"
                uid = rng.choice(active[:100])
                expected = eligible_ids(uid, people, blocks, likes, skips, pairs)
                card = db.present(uid)
                if card is None:
                    assert not expected
                    continue
                target = card[0]["id"]
                assert target in expected
                first = db.react(uid, card[1], "like")
                assert first and not first["matched"]
                likes.add((uid, target))
                if not pairs:
                    await bot.notifications()
                    assert len(sent) == 1 and sent[0][0] == target
                    assert "https://t.me/" not in sent[0][1] and "tg://user" not in sent[0][1]
                reciprocal = db.present(target, "likes")
                assert reciprocal and reciprocal[0]["id"] == uid
                second = db.react(target, reciprocal[1], "like")
                assert second and second["matched"]
                likes.add((target, uid))
                pairs.add(tuple(sorted((uid, target))))
                assert db.react(target, reciprocal[1], "like") is None, "Duplicate callback accepted"

            # Reopen the real SQLite file before sending the queued mutual notices.
            db.close()
            db = Database(path)
            bot = service(db)
            assert db.stats()["matches"] == args.matches
            pending = db.conn.execute(
                "SELECT recipient,other FROM outbox WHERE delivered=0 AND kind='match'"
            ).fetchall()
            expected_notices = {(a, b) for pair in pairs for a, b in (pair, tuple(reversed(pair)))}
            assert {(r[0], r[1]) for r in pending} == expected_notices
            assert len(pending) == 2 * args.matches
            while db.pending_notifications():
                await bot.notifications()
            count = len(sent)
            await bot.notifications()
            assert len(sent) == count, "Queue redelivered completed notifications"
            contacts = Counter()
            for recipient, text in sent[1:]:
                matching = [
                    target
                    for a, target in expected_notices
                    if a == recipient and f"https://t.me/synthetic_{target:04}" in text
                ]
                assert len(matching) == 1, "Wrong contact or non-match message in mutual queue"
                contacts[(recipient, matching[0])] += 1
            assert set(contacts) == expected_notices and all(n == 1 for n in contacts.values())
            report["mutual_matching"] = {
                "pairs": len(pairs),
                "mutual_notifications": len(sent) - 1,
                "duplicates": 0,
                "wrong_contacts": 0,
                "survived_restart": True,
                "one_sided_notice_without_contact": True,
            }

            print(f"Verified {len(pairs)} pairs and {len(sent) - 1} mutual notices; testing feed...", flush=True)
            samples, pool_sizes = [], []
            # Remove prior impressions from the oracle phase; likes/matches remain.
            with db.conn:
                db.conn.execute("DELETE FROM impressions")
            empty = 0
            for index in range(args.requests):
                uid = rng.choice(active)
                expected = eligible_ids(uid, people, blocks, likes, skips, pairs)
                began = time.perf_counter()
                card = db.present(uid)
                samples.append((time.perf_counter() - began) * 1000)
                pool_sizes.append(len(expected))
                if card is None:
                    assert not expected, "Feed incorrectly declared empty"
                    empty += 1
                    continue
                target, token = card[0]["id"], card[1]
                assert target in expected, f"Hard filter violation: {uid}->{target}"
                fresh = {other for other in expected if (uid, other) not in shown}
                if fresh:
                    assert (uid, target) not in shown, "Repeated a card while fresh alternatives existed"
                shown.add((uid, target))
                result = db.react(uid, token, "skip")
                assert result and not result["matched"]
                skips.add((uid, target))
                if (index + 1) % 500 == 0:
                    print(f"Feed: {index + 1}/{args.requests}", flush=True)
            report["realistic_feed"] = {
                **timings(samples),
                "empty_responses": empty,
                "filter_violations": 0,
                "premature_repeats": 0,
                "mean_eligible_pool": round(statistics.mean(pool_sizes), 1),
                "max_eligible_pool": max(pool_sizes),
            }

            # Unlike the previous phase, do not skip or like: only impression
            # history may prevent a previously shown profile from reappearing.
            rotation_pool = eligible_ids(1, people, blocks, likes, skips, pairs)
            with db.conn:
                db.conn.execute("DELETE FROM impressions WHERE actor=1")
            without_reactions = []
            for _ in range(min(25, len(rotation_pool))):
                card = db.present(1)
                assert card and card[0]["id"] in rotation_pool
                assert card[0]["id"] not in without_reactions
                without_reactions.append(card[0]["id"])
            report["rotation_without_reactions"] = {
                "shows": len(without_reactions),
                "unique_profiles": len(set(without_reactions)),
                "eligible_pool": len(rotation_pool),
                "premature_repeats": 0,
            }

            print("Testing simultaneous reuse of one callback...", flush=True)
            # Restore two synthetic members into an otherwise unchanged 1000-user DB.
            with db.conn:
                db.conn.execute("DELETE FROM reactions WHERE actor IN (1,2) OR target IN (1,2)")
                db.conn.execute("DELETE FROM matches WHERE a IN (1,2) OR b IN (1,2)")
                db.conn.execute("DELETE FROM outbox")
                db.conn.execute("INSERT INTO reactions VALUES(2,1,'like',?)", (time.time(),))
            card = db.present(1, "likes")
            assert card and card[0]["id"] == 2

            def duplicate_reaction(_):
                connection = Database(path)
                try:
                    return connection.react(1, card[1], "like")
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(duplicate_reaction, range(2)))
            assert sum(result is not None for result in results) == 1
            assert sum(bool(result and result["matched"]) for result in results) == 1
            assert db.is_match(1, 2)
            assert db.conn.execute("SELECT count(*) FROM outbox WHERE kind='match'").fetchone()[0] == 2
            report["concurrent_duplicate_callback"] = {"attempts": 2, "accepted": 1, "matches": 1, "notifications": 2}

            print("Testing worst case: 999 eligible candidates per request...", flush=True)
            with db.conn:
                for table in ("blocks", "reactions", "matches", "outbox", "views", "impressions"):
                    db.conn.execute(f"DELETE FROM {table}")
                db.conn.execute(
                    "UPDATE users SET active=1,banned=0,consent=1,photo='simulation-photo',age=28,"
                    "seeking='any',min_age=18,max_age=100,local_only=0,"
                    "name=coalesce(name,'Synthetic broad'),gender=coalesce(gender,'other'),"
                    "city=coalesce(city,'Almaty'),city_key=coalesce(city_key,'almaty'),bio=coalesce(bio,'')"
                )
                # Keep 30 real outgoing likes for the personal-history worst case.
                db.conn.executemany(
                    "INSERT INTO reactions VALUES(1,?,'like',?)", [(uid, time.time()) for uid in range(2, 32)]
                )
            worst = []
            for uid in range(32, 32 + args.worst_requests):
                actor = 2 + (uid % (args.users - 1))
                began = time.perf_counter()
                card = db.present(actor)
                worst.append((time.perf_counter() - began) * 1000)
                assert card and card[0]["id"] != actor
            personal = []
            for _ in range(min(50, args.worst_requests)):
                began = time.perf_counter()
                assert db.present(1)
                personal.append((time.perf_counter() - began) * 1000)
            report["broad_pool_feed"] = {**timings(worst), "eligible_pool": args.users - 1}
            report["broad_pool_with_30_likes"] = {**timings(personal), "eligible_pool": args.users - 31}

            db.delete_user(1)
            assert db.user(1) is None
            for table, a, b in (
                ("impressions", "actor", "target"),
                ("reactions", "actor", "target"),
                ("outbox", "recipient", "other"),
            ):
                assert db.conn.execute(f"SELECT count(*) FROM {table} WHERE {a}=1 OR {b}=1").fetchone()[0] == 0
            assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
            assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            report["deletion_and_integrity"] = "passed"
        finally:
            db.close()
    report["temporary_database_deleted"] = not path.exists()
    assert report["temporary_database_deleted"]
    report["duration_seconds"] = round(time.perf_counter() - started, 2)
    report["status"] = "passed"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", type=int, default=1000)
    parser.add_argument("--requests", type=int, default=3000)
    parser.add_argument("--matches", type=int, default=250)
    parser.add_argument("--worst-requests", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--temp-root", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.users < 100 or args.requests < 1 or args.matches < 1 or args.worst_requests < 1:
        parser.error("Use at least 100 users and positive request/match counts")
    report = asyncio.run(simulate(args))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
