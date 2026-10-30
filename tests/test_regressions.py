import asyncio
import time
from datetime import timedelta

from telegram import InlineKeyboardMarkup
from telegram.error import RetryAfter

from tests.conftest import member, react
from tests.test_bot import service


def test_empty_confirmation_nonce_cannot_delete_account(db):
    member(db, 1)
    bot = service(db)

    async def scenario():
        await bot.profile.prompt(1, "delete_confirm", {})
        await bot.profile.accept(1, nonce="", choice="delete")

    asyncio.run(scenario())
    assert db.user(1)["photo"]
    assert db.session(1)[0] == "delete_confirm"


def test_long_emoji_bio_keeps_photo_and_actions(db):
    profile = member(db, 1, name="N" * 40, city="C" * 60, bio="😀" * 500)
    bot = service(db)
    keyboard = InlineKeyboardMarkup([])
    asyncio.run(bot.card(1, profile, keyboard))
    photo = bot.application.bot.send_photo.call_args.kwargs
    text = bot.application.bot.send_message.call_args.kwargs
    assert "😀" not in photo["caption"]
    assert photo["reply_markup"] is None
    assert text["text"] == "😀" * 500
    assert text["reply_markup"] is keyboard


def test_telegram_retry_after_pauses_entire_notification_queue(db):
    member(db, 1)
    member(db, 2)
    react(db, 1)
    react(db, 2)
    bot = service(db)
    bot.application.bot.send_message.side_effect = [RetryAfter(timedelta(seconds=30)), None]

    async def scenario():
        await bot.notifications()
        assert bot.notification_backoff_until > time.monotonic()
        await bot.notifications()
        assert bot.application.bot.send_message.await_count == 1
        # After the queue cooldown, the second member can still receive their match.
        bot.notification_backoff_until = 0
        await bot.notifications()

    asyncio.run(scenario())
    assert bot.application.bot.send_message.await_count == 2
    assert db.conn.execute("SELECT count(*) FROM outbox WHERE delivered=0").fetchone()[0] == 1
