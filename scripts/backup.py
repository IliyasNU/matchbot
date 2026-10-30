"""Consistent SQLite backup, including committed WAL data, while the bot runs."""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data" / "bot.sqlite3")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "backups" / (datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + ".sqlite3"),
    )
    args = parser.parse_args()
    if not args.source.is_file():
        parser.error("Source database does not exist")
    if args.source.resolve() == args.output.resolve() or args.output.exists():
        parser.error("Output must be a new file distinct from the source")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(args.source.resolve().as_uri() + "?mode=ro", uri=True)
    destination = sqlite3.connect(args.output)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    print(f"Backup saved: {args.output}")


if __name__ == "__main__":
    main()
