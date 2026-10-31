"""Compare discovery before/after on deterministic, isolated populations.

Example: python scripts/benchmark_scale.py --users 10000 --requests 30
  --baseline-ref a050d8832be8d4824d45eec3d9e4a175ab8a7fd6 --report data/scale.json

Only local Git history, temporary SQLite databases and worker processes are used.
No .env, production database, Telegram token or network connection is used.
"""

import argparse
import importlib
import json
import os
import platform
import random
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
NOW = 1_800_000_000.0
SNAPSHOT_FILES = ("db/__init__.py", "db/models.py", "db/database.py", "matching.py")
INTERESTS = (
    "music",
    "movies",
    "books",
    "gaming",
    "sports",
    "travel",
    "art",
    "tech",
    "cooking",
    "nature",
    "study",
    "fitness",
)


def latency(values):
    ordered = sorted(values)
    return {
        "requests": len(values),
        "mean_ms": round(statistics.mean(values), 3),
        **{f"p{p}_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * p / 100))], 3) for p in (50, 95, 99)},
        "max_ms": round(ordered[-1], 3),
    }


def population(count, seed):
    rng = random.Random(seed)
    people = {}
    for uid in range(1, count + 1):
        city = rng.choice(("Almaty", "Astana", "Shymkent", "Kyzylorda", "Karaganda"))
        people[uid] = dict(
            id=uid,
            username=f"synthetic_{uid:05}",
            lang="en",
            name=f"Synthetic person {uid:05}",
            age=rng.randint(18, 65),
            gender=rng.choice(("male", "female", "other")),
            seeking="any",
            city=city,
            city_key=city.casefold(),
            bio="Synthetic benchmark biography. " + "x" * 469,
            photo="synthetic-photo-" + str(uid) + "-" + "x" * 100,
            min_age=18,
            max_age=100,
            local_only=0,
            active=1,
            banned=0,
            reachable=1,
            consent=1,
            created=NOW - 86400,
            last_request=0,
            interests=json.dumps(rng.sample(INTERESTS, rng.randint(1, 5))),
            purpose=rng.choice(("any", "friendship", "relationship", "chat")),
            last_active=NOW - rng.randint(0, 60) * 86400,
        )
    people[1].update(
        age=28, gender="male", city="Almaty", city_key="almaty", interests='["books", "travel"]', purpose="friendship"
    )
    return people


def insert_population(db, people):
    columns = tuple(next(iter(people.values())))
    with db.conn:
        db.conn.executemany(
            f"INSERT INTO users({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            [tuple(person[column] for column in columns) for person in people.values()],
        )


def expected_ids(people, blocks=(), reactions=(), pairs=()):
    """Fixture-only hard-filter oracle; never call Database.eligible here."""
    me = people[1]
    excluded = set(reactions) | set(pairs) | set(blocks)
    result = set()
    for uid, person in people.items():
        if (
            uid == 1
            or uid in excluded
            or not person["active"]
            or person["banned"]
            or not person["photo"]
            or not person["consent"]
        ):
            continue
        if (
            not me["min_age"] <= person["age"] <= me["max_age"]
            or not person["min_age"] <= me["age"] <= person["max_age"]
        ):
            continue
        if me["seeking"] not in ("any", person["gender"]) or person["seeking"] not in ("any", me["gender"]):
            continue
        if (me["local_only"] or person["local_only"]) and me["city_key"] != person["city_key"]:
            continue
        result.add(uid)
    return result


def selective_population(db, people, seed):
    rng = random.Random(seed + 1)
    for uid, person in people.items():
        person.update(
            seeking=rng.choice(("any", "male", "female", "other")),
            min_age=max(18, person["age"] - 8),
            max_age=min(100, person["age"] + 12),
            local_only=int(rng.random() < 0.25),
            active=int(uid % 17 != 0),
            banned=int(uid % 29 == 0),
        )
    people[1].update(seeking="female", min_age=24, max_age=35, local_only=1, active=1, banned=0)
    for uid in range(32, 44):
        people[uid].update(
            age=27,
            gender="female",
            seeking="male",
            min_age=18,
            max_age=100,
            local_only=0,
            active=1,
            banned=0,
            city="Almaty",
            city_key="almaty",
        )
    people[33]["age"] = 60
    people[34]["gender"] = "male"
    people[35]["seeking"] = "female"
    people[36].update(city="Astana", city_key="astana")
    people[37]["active"] = 0
    people[38]["banned"] = 1
    people[39].update(min_age=40, max_age=60)
    columns = ("age", "gender", "seeking", "min_age", "max_age", "local_only", "active", "banned", "city", "city_key")
    with db.conn:
        for table in ("reactions", "matches", "blocks", "impressions", "views", "outbox"):
            db.conn.execute(f"DELETE FROM {table}")
        db.conn.executemany(
            "UPDATE users SET " + ",".join(f"{column}=?" for column in columns) + " WHERE id=?",
            [tuple(person[column] for column in columns) + (uid,) for uid, person in people.items()],
        )
        db.conn.execute("INSERT INTO blocks VALUES(40,1)")
        db.conn.execute("INSERT INTO reactions VALUES(1,41,'skip',?)", (NOW,))
        db.conn.execute("INSERT INTO matches VALUES(1,42,?)", (NOW,))
        db.conn.execute("INSERT INTO reactions VALUES(1,43,'like',?)", (NOW,))
    return expected_ids(people, blocks={40}, reactions={41, 43}, pairs={42})


def measure_case(db, expected, requests):
    selected = set()

    def query():
        profile = db.candidates(1)
        if not expected:
            assert profile is None, "Returned a profile when oracle pool was empty"
        else:
            assert profile and profile["id"] in expected, "Strict eligibility violation"
            assert len(profile["bio"]) == 500 and profile["photo"].startswith("synthetic-photo-")
            selected.add(profile["id"])

    for _ in range(3):
        query()
    samples = []
    for _ in range(requests):
        started = time.perf_counter()
        query()
        samples.append((time.perf_counter() - started) * 1000)
    # Keep allocation profiling out of the latency samples: tracemalloc changes
    # the cost of Python-heavy code and would distort a before/after comparison.
    tracemalloc.start()
    try:
        query()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return {
        **latency(samples),
        "eligible_pool": len(expected),
        "peak_python_kib": round(peak / 1024, 1),
        "distinct_selected_including_warmup": len(selected),
        "filter_violations": 0,
    }


def worker(args):
    sys.path.insert(0, str(args.code_root.resolve()))
    module = importlib.import_module("db.database")
    module.time.time = lambda: NOW
    random.seed(args.seed)
    people = population(args.users, args.seed)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="scale-worker-", dir=args.temp_root) as temporary:
        path = Path(temporary) / "population.sqlite3"
        db = module.Database(path)
        try:
            insert_population(db, people)
            assert db.conn.execute("SELECT count(*) FROM users").fetchone()[0] == args.users
            cases = {}
            for label in ("broad", "broad_with_30_likes", "selective"):
                if label == "broad":
                    expected = expected_ids(people)
                elif label == "broad_with_30_likes":
                    with db.conn:
                        db.conn.executemany(
                            "INSERT INTO reactions VALUES(1,?,'like',?)", [(uid, NOW) for uid in range(2, 32)]
                        )
                    expected = expected_ids(people, reactions=set(range(2, 32)))
                else:
                    expected = selective_population(db, people, args.seed)
                print(f"{args.label}: {args.users} users / {label} / {len(expected)} eligible", flush=True)
                cases[label] = measure_case(db, expected, args.requests)
            assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            db.close()
    result = {
        "users": args.users,
        "requests_per_case": args.requests,
        "cases": cases,
        "temporary_database_deleted": not path.exists(),
        "duration_seconds": round(time.perf_counter() - started, 2),
    }
    assert result["temporary_database_deleted"]
    args.result_file.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def baseline_snapshot(ref, destination):
    sha = subprocess.check_output(
        ["git", "rev-parse", "--verify", "--end-of-options", ref + "^{commit}"], cwd=ROOT, text=True
    ).strip()
    for name in SNAPSHOT_FILES:
        content = subprocess.check_output(["git", "show", f"{sha}:{name}"], cwd=ROOT)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return sha


def run_worker(args, root, label, temporary):
    result_path = temporary / f"{label}.json"
    command = [
        sys.executable,
        str(SCRIPT),
        "--worker",
        "--code-root",
        str(root),
        "--label",
        label,
        "--users",
        str(args.users),
        "--requests",
        str(args.requests),
        "--seed",
        str(args.seed),
        "--result-file",
        str(result_path),
        "--temp-root",
        str(temporary),
    ]
    subprocess.run(command, cwd=ROOT, check=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    return json.loads(result_path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", type=int, default=10000)
    parser.add_argument("--requests", type=int, default=30)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--baseline-ref")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--temp-root", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--code-root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--label", default="working_tree", help=argparse.SUPPRESS)
    parser.add_argument("--result-file", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.users < 100 or args.requests < 1:
        parser.error("Use at least 100 users and a positive request count")
    if args.worker:
        if args.result_file is None:
            parser.error("Internal worker requires --result-file")
        worker(args)
        return
    with tempfile.TemporaryDirectory(prefix="matchbot-benchmark-", dir=args.temp_root) as temporary_name:
        temporary = Path(temporary_name)
        report = {
            "seed": args.seed,
            "users": args.users,
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
            "timing_scope": "sequential local Database.candidates; no Telegram/network; no .env or production data",
            "memory_scope": "peak tracemalloc Python allocations for one extra query; excluded from timed samples",
            "clock_unix": NOW,
            "status": "passed",
        }
        if args.baseline_ref:
            baseline_root = temporary / "baseline"
            report["baseline_ref"] = baseline_snapshot(args.baseline_ref, baseline_root)
            report["before"] = run_worker(args, baseline_root, "before", temporary)
        report["after"] = run_worker(args, ROOT, "after", temporary)
        if "before" in report:
            report["comparisons"] = {}
            for name, after in report["after"]["cases"].items():
                before = report["before"]["cases"][name]
                assert before["eligible_pool"] == after["eligible_pool"], "Before/after fixture pools differ"
                report["comparisons"][name] = {
                    "mean_speedup": round(before["mean_ms"] / max(after["mean_ms"], 0.001), 2),
                    "p95_speedup": round(before["p95_ms"] / max(after["p95_ms"], 0.001), 2),
                    "python_peak_reduction": round(before["peak_python_kib"] / max(after["peak_python_kib"], 0.1), 2),
                }
    report["temporary_files_deleted"] = not temporary.exists()
    assert report["temporary_files_deleted"]
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
