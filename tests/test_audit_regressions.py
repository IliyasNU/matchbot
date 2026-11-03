"""Failures reproduced during the full project audit, without live Telegram calls."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from db.models import MAX_DATABASE_ID, parse_integer
from tests.conftest import member, react
from tests.test_bot import service


def pair(db, first, second):
    for uid in (first, second):
        member(db, uid, city=f"City {first}")
        db.set_filters(uid, 18, 100, True)
    react(db, first)
    assert react(db, second, mode="likes")["matched"]


@pytest.mark.parametrize("change", ["block", "delete", "ban_target", "ban_viewer"])
def test_matches_recheck_access_after_awaiting_photo(db, change):
    pair(db, 1, 2)
    bot = service(db)

    async def photo(*args, **kwargs):
        if change == "block":
            db.block_match(2, 1)
        elif change == "delete":
            db.delete_user(2)
        else:
            db.ban(2 if change == "ban_target" else 1)

    bot.application.bot.send_photo.side_effect = photo
    asyncio.run(bot.show_matches(1))
    assert not any("https://t.me/" in call.kwargs["text"] for call in bot.application.bot.send_message.call_args_list)


def test_matches_use_current_username_after_awaiting_photo(db):
    pair(db, 1, 2)
    bot = service(db)

    async def photo(*args, **kwargs):
        db.touch(2, "new_username")

    bot.application.bot.send_photo.side_effect = photo
    asyncio.run(bot.show_matches(1))
    texts = [call.kwargs["text"] for call in bot.application.bot.send_message.call_args_list]
    assert any("https://t.me/new_username" in text for text in texts)
    assert not any("https://t.me/user2" in text for text in texts)


def test_cancelled_notification_snapshot_cannot_send_a_new_match_twice(db):
    pair(db, 1, 2)
    pair(db, 3, 4)
    bot = service(db)
    sent = []

    async def send(chat_id, text, **kwargs):
        sent.append((chat_id, text))
        if len(sent) == 1:
            assert db.block_match(3, 4)
            assert db.unblock(3, 4)
            react(db, 3)
            assert react(db, 4, mode="likes")["matched"]

    bot.application.bot.send_message.side_effect = send

    async def scenario():
        await bot.notifications()
        assert {uid for uid, _ in sent} == {1, 2}
        await bot.notifications()
        assert sorted(uid for uid, _ in sent) == [1, 2, 3, 4]
        assert not db.pending_notifications()

    asyncio.run(scenario())


def test_completed_notification_is_no_longer_valid(db):
    pair(db, 1, 2)
    item = db.pending_notifications()[0]
    assert db.notification_valid(item)
    db.notification_done(item["id"])
    assert not db.notification_valid(item)


def test_paused_matches_keep_contact_access(db):
    pair(db, 1, 2)
    db.set_user(2, active=0)
    bot = service(db)
    asyncio.run(bot.show_matches(1))
    assert any("https://t.me/user2" in call.kwargs["text"] for call in bot.application.bot.send_message.call_args_list)


def test_pending_notification_is_rechecked_when_deferred(db):
    pair(db, 1, 2)
    item = db.pending_notifications()[0]
    db.notification_retry(item["id"], 60)
    assert not db.notification_valid(item)


def test_notification_deferred_during_another_send_is_preserved_for_retry(db):
    pair(db, 1, 2)
    bot = service(db)
    deferred = next(item for item in db.pending_notifications() if item["recipient"] == 2)
    recipients = []

    async def send(chat_id, text, **kwargs):
        recipients.append(chat_id)
        if chat_id == 1:
            db.notification_retry(deferred["id"], 60)

    bot.application.bot.send_message.side_effect = send

    async def scenario():
        await bot.notifications()
        assert recipients == [1]
        row = db.conn.execute("SELECT * FROM outbox WHERE id=?", (deferred["id"],)).fetchone()
        assert not row["delivered"] and row["attempts"] == 1
        with db.conn:
            db.conn.execute("UPDATE outbox SET due=0 WHERE id=?", (deferred["id"],))
        await bot.notifications()
        assert recipients == [1, 2]

    asyncio.run(scenario())


def test_integer_parser_handles_sqlite_limits_and_zero_page_offset():
    assert parse_integer(str(MAX_DATABASE_ID)) == MAX_DATABASE_ID
    assert parse_integer(str(MAX_DATABASE_ID + 1)) is None
    assert parse_integer("0") is None
    assert parse_integer("0", minimum=0, maximum=100000) == 0
    assert parse_integer("100000", minimum=0, maximum=100000) == 100000
    assert parse_integer("100001", minimum=0, maximum=100000) is None
    assert parse_integer("1" * 5000) is None


@pytest.mark.parametrize("prefix", ["unmatch", "unblock", "matchreport", "blocked", "matches"])
@pytest.mark.parametrize("number", ["9" * 40, "²", "-1", "2:extra"])
def test_malformed_numeric_callbacks_do_not_crash_or_change_data(db, prefix, number):
    pair(db, 1, 2)
    bot = service(db)
    query = SimpleNamespace(data=f"{prefix}:{number}", answer=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1, username="user1", language_code="en"),
        effective_chat=SimpleNamespace(id=1, type="private"),
        effective_message=SimpleNamespace(text=None),
        callback_query=query,
    )
    asyncio.run(bot.handle(update, None))
    assert db.is_match(1, 2)
    assert not db.blocked(1, 2)
    assert not db.reports()


@pytest.mark.parametrize("command", ["inspect", "ban", "unban", "resolve"])
@pytest.mark.parametrize("number", ["9" * 40, "²", "0", "-1"])
def test_invalid_admin_ids_produce_usage_instead_of_database_error(db, command, number):
    bot = service(db)
    asyncio.run(bot.admin(99, command, [number]))
    assert bot.application.bot.send_message.call_args.kwargs["text"].startswith("Usage:")


def test_ban_during_callback_acknowledgement_is_applied_before_command(db):
    pair(db, 1, 2)
    bot = service(db)

    async def acknowledge():
        db.ban(1)

    query = SimpleNamespace(data="nav:resume", answer=AsyncMock(side_effect=acknowledge))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1, username="user1", language_code="en"),
        effective_chat=SimpleNamespace(id=1, type="private"),
        effective_message=SimpleNamespace(text=None),
        callback_query=query,
    )
    asyncio.run(bot.handle(update, None))
    assert db.user(1)["banned"] and not db.user(1)["active"]
