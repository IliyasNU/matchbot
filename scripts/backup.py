"""Consistent SQLite backup, including committed WAL data, while the bot runs."""

import argparse
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def create_backup(source, output):
    source = Path(source).resolve(strict=True)
    output = Path(output).resolve()
    if not source.is_file():
        raise ValueError("Source must be a database file")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Claim the name exclusively, including when another backup starts after
    # validation. sqlite3.connect alone would silently open an existing file.
    with output.open("xb"):
        pass
    try:
        with (
            closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as connection,
            closing(sqlite3.connect(output)) as destination,
        ):
            connection.backup(destination)
    except BaseException:
        # A failed copy must not look like a usable restore point.
        output.unlink(missing_ok=True)
        raise
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data" / "bot.sqlite3")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "backups" / (datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + ".sqlite3"),
    )
    args = parser.parse_args()
    try:
        output = create_backup(args.source, args.output)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Backup failed: {exc}\n")
    print(f"Backup saved: {output}")


if __name__ == "__main__":
    main()
