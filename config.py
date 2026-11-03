import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from db.models import parse_integer

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    token: str
    database: Path
    admin_ids: frozenset[int] = frozenset()
    skip_days: int = 7
    max_concurrent_updates: int = 32

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env")
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token or token == "YOUR_BOT_TOKEN_HERE":
            raise ValueError("Set BOT_TOKEN in .env using the token from @BotFather.")
        bot_id, _, secret = token.partition(":")
        if parse_integer(bot_id) is None or not re.fullmatch(r"[A-Za-z0-9_-]+", secret):
            raise ValueError("BOT_TOKEN has an invalid format. Copy the full @BotFather token.")
        try:
            admins = frozenset(
                parse_integer(value.strip()) for value in os.getenv("ADMIN_IDS", "").split(",") if value.strip()
            )
            days = int(os.getenv("SKIP_COOLDOWN_DAYS", "7"))
            concurrency = int(os.getenv("MAX_CONCURRENT_UPDATES", "32"))
            if days < 1 or None in admins or not 1 <= concurrency <= 128:
                raise ValueError()
        except ValueError as exc:
            raise ValueError(
                "ADMIN_IDS must be positive numeric IDs; SKIP_COOLDOWN_DAYS must be >= 1; "
                "MAX_CONCURRENT_UPDATES must be between 1 and 128."
            ) from exc
        path = Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3"))
        return cls(token, path if path.is_absolute() else ROOT / path, admins, days, concurrency)
