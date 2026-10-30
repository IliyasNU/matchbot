import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    token: str
    database: Path
    admin_ids: frozenset[int] = frozenset()
    skip_days: int = 7

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env")
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token or token == "YOUR_BOT_TOKEN_HERE":
            raise ValueError("Set BOT_TOKEN in .env using the token from @BotFather.")
        if ":" not in token or not token.split(":", 1)[0].isdigit():
            raise ValueError("BOT_TOKEN has an invalid format. Copy the full @BotFather token.")
        try:
            admins = frozenset(int(value.strip()) for value in os.getenv("ADMIN_IDS", "").split(",") if value.strip())
            days = int(os.getenv("SKIP_COOLDOWN_DAYS", "7"))
            if days < 1 or any(uid <= 0 for uid in admins):
                raise ValueError()
        except ValueError as exc:
            raise ValueError("ADMIN_IDS must be positive numeric IDs; SKIP_COOLDOWN_DAYS must be >= 1.") from exc
        path = Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3"))
        return cls(token, path if path.is_absolute() else ROOT / path, admins, days)
