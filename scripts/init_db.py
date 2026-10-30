"""Initialize the local SQLite schema without a Telegram token."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.database import Database  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=ROOT / "data" / "bot.sqlite3")
    args = parser.parse_args()
    db = Database(args.path)
    db.close()
    print(f"Database ready: {args.path}")


if __name__ == "__main__":
    main()
