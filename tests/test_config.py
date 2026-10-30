import pytest

import config
from config import ROOT, Settings


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda path: None)
    for key in ("BOT_TOKEN", "DATABASE_PATH", "ADMIN_IDS", "SKIP_COOLDOWN_DAYS"):
        monkeypatch.delenv(key, raising=False)


def test_token_required():
    with pytest.raises(ValueError, match="BOT_TOKEN"):
        Settings.from_env()


def test_paths_resolve_against_project(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:test-token")
    monkeypatch.setenv("ADMIN_IDS", "12, 34")
    settings = Settings.from_env()
    assert settings.database == ROOT / "data" / "bot.sqlite3"
    assert settings.admin_ids == {12, 34}


@pytest.mark.parametrize("key,value", [("ADMIN_IDS", "name"), ("ADMIN_IDS", "-1"), ("SKIP_COOLDOWN_DAYS", "0")])
def test_invalid_config(key, value, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:test-token")
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        Settings.from_env()
