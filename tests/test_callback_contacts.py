"""Button actions never interpret bot text; contacts use a fresh Telegram lookup."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.error import BadRequest, NetworkError, RetryAfter

from tests.conftest import member, react
from tests.test_audit_regressions import pair
from tests.test_bot import service


def callback(data, text="/pause"):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=1, username="user1", language_code="en"),
        effective_chat=SimpleNamespace(id=1, type="private"),
        effective_message=SimpleNamespace(text=text),
        message=None,
        callback_query=SimpleNamespace(data=data, answer=AsyncMock(), edit_message_reply_markup=AsyncMock()),
    )


@pytest.mark.parametrize("text", ["/pause", "/delete", "/ban 2", "/start@another_bot"])
def test_card_callback_never_executes_command_from_bot_message(db, text):
    member(db, 1)
    member(db, 2)
    bot = service(db)
    bot.schedule_notifications = Mock()
    token = db.present(1)[1]
    asyncio.run(bot.handle(callback(f"r:{token}:like", text), None))
    assert db.user(1)["active"]
    assert db.session(1) is None
    assert db.conn.execute("SELECT kind FROM reactions WHERE actor=1 AND target=2").fetchone()[0] == "like"
    assert not db.user(2)["banned"]


def test_unblock_callback_ignores_blocked_persons_command_name(db):
    pair(db, 1, 2)
    db.block_match(1, 2)
    bot = service(db)
    asyncio.run(bot.handle(callback("unblock:2", "/pause"), None))
    assert db.user(1)["active"]
    assert not db.blocked(1, 2)


def test_direct_message_still_executes_its_command(db):
    member(db, 1)
    bot = service(db)
    update = callback("")
    update.callback_query = None
    update.message = update.effective_message
    asyncio.run(bot.handle(update, None))
    assert not db.user(1)["active"]


@pytest.mark.parametrize("error", [BadRequest("Query is too old"), NetworkError("offline")])
def test_failed_acknowledgement_does_not_discard_valid_like(db, error, caplog):
    member(db, 1)
    member(db, 2)
    bot = service(db)
    bot.schedule_notifications = Mock()
    token = db.present(1)[1]
    update = callback(f"r:{token}:like", text=None)
    update.callback_query.answer.side_effect = error
    asyncio.run(bot.handle(update, None))
    assert db.conn.execute("SELECT kind FROM reactions WHERE actor=1 AND target=2").fetchone()[0] == "like"
    assert any("acknowledgement" in record.message for record in caplog.records)
    # Replayed updates still cannot reuse the consumed token.
    db.rate_allowed = lambda uid: True
    asyncio.run(bot.handle(update, None))
    assert db.conn.execute("SELECT count(*) FROM reactions").fetchone()[0] == 1


@pytest.mark.parametrize("location", ["matches", "notifications"])
@pytest.mark.parametrize("username", ["fresh_username", None])
def test_contact_uses_current_telegram_username_without_new_incoming_message(db, location, username):
    pair(db, 1, 2)
    bot = service(db)
    bot.application.bot.get_chat = AsyncMock(
        side_effect=lambda uid: SimpleNamespace(id=uid, type="private", username=username)
    )
    asyncio.run(bot.show_matches(1) if location == "matches" else bot.notifications())
    texts = [call.kwargs["text"] for call in bot.application.bot.send_message.call_args_list]
    contact = "https://t.me/fresh_username" if username else "tg://user?id=2"
    assert any(contact in text for text in texts)
    assert all("https://t.me/user2" not in text for text in texts)
    bot.application.bot.get_chat.assert_any_await(2)


def test_contact_lookup_failure_falls_back_to_id_without_reusing_stale_username(db):
    pair(db, 1, 2)
    bot = service(db)
    bot.application.bot.get_chat = AsyncMock(side_effect=NetworkError("offline"))
    asyncio.run(bot.show_matches(1))
    texts = [call.kwargs["text"] for call in bot.application.bot.send_message.call_args_list]
    assert any("tg://user?id=2" in text for text in texts)
    assert all("https://t.me/user2" not in text for text in texts)


@pytest.mark.parametrize("location", ["matches", "notifications"])
def test_block_during_contact_lookup_prevents_sending_contact(db, location):
    pair(db, 1, 2)
    bot = service(db)

    async def get_chat(uid):
        db.block_match(2, 1)
        return SimpleNamespace(id=uid, type="private", username="fresh_username")

    bot.application.bot.get_chat = AsyncMock(side_effect=get_chat)
    asyncio.run(bot.show_matches(1) if location == "matches" else bot.notifications())
    texts = [call.kwargs["text"] for call in bot.application.bot.send_message.call_args_list]
    assert not any("https://t.me/" in text or "tg://user?id=" in text for text in texts)


def test_one_sided_like_does_not_lookup_or_share_contact(db):
    member(db, 1)
    member(db, 2)
    react(db, 1)
    bot = service(db)
    bot.application.bot.get_chat = AsyncMock()
    asyncio.run(bot.notifications())
    bot.application.bot.get_chat.assert_not_awaited()
    assert not any("https://t.me/" in call.kwargs["text"] for call in bot.application.bot.send_message.call_args_list)


@pytest.mark.parametrize("chat_id,chat_type", [(3, "private"), (2, "group")])
def test_contact_lookup_checks_identity_before_using_username(db, chat_id, chat_type):
    pair(db, 1, 2)
    bot = service(db)
    bot.application.bot.get_chat = AsyncMock(
        return_value=SimpleNamespace(id=chat_id, type=chat_type, username="someone_else")
    )
    asyncio.run(bot.show_matches(1))
    texts = [call.kwargs["text"] for call in bot.application.bot.send_message.call_args_list]
    assert any("tg://user?id=2" in text for text in texts)
    assert not any("someone_else" in text for text in texts)


def test_contact_lookup_retry_after_defers_outbox_and_stops_sender(db):
    pair(db, 1, 2)
    bot = service(db)
    bot.application.bot.get_chat = AsyncMock(side_effect=RetryAfter(timedelta(seconds=30)))

    async def scenario():
        await bot.notifications()
        await bot.notifications()

    asyncio.run(scenario())
    bot.application.bot.get_chat.assert_awaited_once()
    bot.application.bot.send_message.assert_not_awaited()
    rows = db.conn.execute("SELECT * FROM outbox ORDER BY id").fetchall()
    assert len(rows) == 2 and not any(row["delivered"] for row in rows)
    assert sum(row["attempts"] for row in rows) == 1
    assert bot.notification_backoff_until > 0


def test_report_reasons_and_submission_survive_later_target_changes(db):
    member(db, 1)
    member(db, 2)
    bot = service(db)
    db.rate_allowed = lambda uid: True
    token = db.present(1)[1]
    db.set_user(2, active=0)

    async def scenario():
        await bot.handle(callback(f"r:{token}:report"), None)
        assert not db.reports()
        db.set_user(2, lang="ru")
        await bot.handle(callback(f"r:{token}:report_underage"), None)

    asyncio.run(scenario())
    assert db.blocked(1, 2)
    assert db.reports()[0]["reason"] == "underage"
    assert db.user(1)["active"]
