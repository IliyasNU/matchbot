import asyncio
from types import SimpleNamespace

import pytest
from telegram import Update
from telegram.error import NetworkError
from telegram.ext import ChatMemberHandler

from tests.conftest import member
from tests.test_bot import service


def membership_update(uid, new_status, chat_type="private"):
    user = {"id": uid, "is_bot": False, "first_name": "Member"}
    bot = {"id": 123456, "is_bot": True, "first_name": "Matchbot"}
    member_status = {"user": bot, "status": "member"}
    blocked_status = {"user": bot, "status": "kicked", "until_date": 0}
    return Update.de_json(
        {
            "update_id": 1,
            "my_chat_member": {
                "chat": {"id": uid if chat_type == "private" else -1, "type": chat_type},
                "from": user,
                "date": 1000,
                "old_chat_member": member_status if new_status == "kicked" else blocked_status,
                "new_chat_member": blocked_status if new_status == "kicked" else member_status,
            },
        },
        None,
    )


def test_blocking_bot_hides_profile_and_unblocking_requires_explicit_resume(db):
    member(db, 1)
    member(db, 2)
    app = service(db)
    _, token = db.present(1)
    handler = next(h for h in app.build().handlers[0] if isinstance(h, ChatMemberHandler))
    app.application = service(db).application

    async def scenario():
        blocked = membership_update(2, "kicked")
        assert handler.check_update(blocked)
        await handler.callback(blocked, None)
        assert not db.user(2)["reachable"] and not db.user(2)["active"]
        assert db.candidates(1) is None
        assert db.react(1, token, "like") is None
        assert db.view(1, token)  # Reporting a previously shown profile is still possible.
        await handler.callback(membership_update(2, "member"), None)
        assert db.user(2)["reachable"] and not db.user(2)["active"]
        await app.command(2, "resume")
        assert db.candidates(1)["id"] == 2

    asyncio.run(scenario())


def test_membership_updates_ignore_groups_and_do_not_create_profiles(db):
    member(db, 1)
    bot = service(db)

    async def scenario():
        await bot.handle_membership(membership_update(1, "kicked", "group"), None)
        await bot.handle_membership(membership_update(2, "member"), None)

    asyncio.run(scenario())
    assert db.user(1)["active"]
    assert db.user(2) is None


def test_match_list_sends_one_profile_at_a_time_with_navigation(db):
    for uid in range(1, 8):
        member(db, uid)
    with db.conn:
        db.conn.executemany("INSERT INTO matches VALUES(1,?,?)", [(uid, uid) for uid in range(2, 8)])
    bot = service(db)
    telegram = bot.application.bot

    async def scenario():
        await bot.show_matches(1)
        telegram.send_photo.assert_awaited_once()
        telegram.send_message.assert_awaited_once()
        first = telegram.send_message.call_args.kwargs
        assert "https://t.me/user7" in first["text"]
        assert {button.callback_data for row in first["reply_markup"].inline_keyboard for button in row} == {
            "unmatch:7",
            "matchreport:7",
            "matches:1",
            "nav:menu",
        }
        await bot.show_matches(1, offset=1)
        assert telegram.send_photo.await_count == telegram.send_message.await_count == 2
        second = telegram.send_message.call_args.kwargs
        assert "https://t.me/user6" in second["text"]
        assert "matches:0" in {b.callback_data for b in second["reply_markup"].inline_keyboard[-1]}

    asyncio.run(scenario())


def test_error_log_has_stack_location_without_sensitive_exception_text(db, caplog):
    bot = service(db)

    def failing_api_call():
        raise NetworkError("https://api.telegram.org/bot123456:secret-example/sendMessage")

    with pytest.raises(NetworkError) as raised:
        failing_api_call()
    asyncio.run(bot.on_error(None, SimpleNamespace(error=raised.value)))
    assert "NetworkError" in caplog.text and "failing_api_call" in caplog.text
    assert "secret-example" not in caplog.text
