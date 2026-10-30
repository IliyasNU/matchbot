import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters
from telegram.request import BaseRequest

from bot import MatchBot, contact_link, instance_lock
from config import Settings
from i18n import TEXT
from tests.conftest import member, react


def service(db):
    bot = MatchBot(db, Settings("123456:fake-test-token", ":memory:", frozenset({99})))
    bot.application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock()))
    return bot


def test_notifications_send_both_once(db):
    member(db, 1)
    member(db, 2)
    db.set_user(2, lang="ru")
    react(db, 1)
    react(db, 2)
    bot = service(db)

    async def scenario():
        await bot.notifications()
        await bot.notifications()

    asyncio.run(scenario())
    calls = bot.application.bot.send_message.call_args_list
    assert len(calls) == 2
    assert {c.kwargs["chat_id"] for c in calls} == {1, 2}
    assert "It's a match" in calls[0].kwargs["text"]
    assert "Взаимная симпатия" in calls[1].kwargs["text"]
    assert not db.pending_notifications()


@pytest.mark.parametrize("error", [NetworkError("offline"), RetryAfter(timedelta(seconds=30))])
def test_transient_notification_errors_are_retried(db, error):
    member(db, 1)
    member(db, 2)
    react(db, 1)
    bot = service(db)
    bot.application.bot.send_message.side_effect = error
    asyncio.run(bot.notifications())
    item = db.conn.execute("SELECT * FROM outbox").fetchone()
    assert item["attempts"] == 1 and not item["delivered"] and item["due"] > 0


def test_one_unreachable_user_does_not_prevent_other_notification(db):
    member(db, 1)
    member(db, 2)
    react(db, 1)
    react(db, 2)
    bot = service(db)
    bot.application.bot.send_message.side_effect = [Forbidden("blocked"), None]
    asyncio.run(bot.notifications())
    assert bot.application.bot.send_message.await_count == 2
    assert not db.user(1)["active"]
    assert db.user(2)["active"]


def test_admin_rejects_non_admin(db):
    member(db, 1)
    member(db, 2)
    bot = service(db)
    asyncio.run(bot.admin(1, "ban", ["2"]))
    assert not db.user(2)["banned"]
    asyncio.run(bot.admin(99, "ban", ["2"]))
    assert db.user(2)["banned"]


def test_profile_caption_escapes_html_and_handles_bad_photo(db):
    profile = member(db, 1, name="<b>Test</b>", bio="<a href='bad'>hi</a>")
    bot = service(db)
    bot.application.bot.send_photo.side_effect = BadRequest("invalid file id")
    asyncio.run(bot.card(1, profile))
    text = bot.application.bot.send_message.call_args.kwargs["text"]
    assert "&lt;b&gt;Test" in text and "&lt;a" in text


def test_contact_fallback_and_no_user_html_in_link():
    assert "https://t.me/example" in contact_link({"id": 1, "username": "example"}, "en")
    assert "tg://user?id=1" in contact_link({"id": 1, "username": None}, "ru")


def test_translation_keys_and_placeholders_match():
    import string

    formatter = string.Formatter()
    for key, (en, ru) in TEXT.items():
        assert en and ru, key
        assert {f for _, f, _, _ in formatter.parse(en) if f} == {f for _, f, _, _ in formatter.parse(ru) if f}, key


def test_instance_lock_prevents_second_process(tmp_path):
    path = tmp_path / "bot.lock"
    with instance_lock(path), pytest.raises(RuntimeError), instance_lock(path):
        pass
    with instance_lock(path):
        pass


class FakeTelegram(BaseRequest):
    """Exercise the real Telegram SDK and dispatcher without network or messages."""

    def __init__(self):
        self.calls = []
        self.message_id = 0

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((endpoint, params))
        if endpoint == "getMe":
            result = {"id": 123456, "is_bot": True, "first_name": "MatchBot", "username": "test_match_bot"}
        elif endpoint in {"sendMessage", "sendPhoto", "editMessageReplyMarkup"}:
            self.message_id += 1
            result = {
                "message_id": self.message_id,
                "date": 1000,
                "chat": {"id": int(params.get("chat_id", 1)), "type": "private"},
                "text": params.get("text", ""),
            }
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


def test_end_to_end_registration_matching_restart_edit_delete(db):
    async def scenario():
        transport = FakeTelegram()
        service_bot = MatchBot(db, Settings("123456:fake-token", ":memory:"))
        app = Application.builder().token("123456:fake-token").request(transport).build()
        service_bot.application = app
        db.rate_allowed = lambda uid: True
        app.add_handler(CallbackQueryHandler(service_bot.handle))
        app.add_handler(MessageHandler(filters.ALL, service_bot.handle))
        failures = []

        async def error_handler(update, context):
            failures.append(context.error)

        app.add_error_handler(error_handler)
        counter = 0

        async def dispatch(uid, text=None, callback=None, photo=False, chat_type="private"):
            nonlocal counter
            counter += 1
            user = {"id": uid, "is_bot": False, "first_name": f"User{uid}", "language_code": "ru" if uid == 2 else "en"}
            message = {"message_id": counter, "date": 1000, "chat": {"id": uid, "type": chat_type}, "from": user}
            if photo:
                message["photo"] = [
                    {"file_id": f"photo{uid}", "file_unique_id": f"unique{uid}", "width": 100, "height": 100}
                ]
            if text:
                message["text"] = text
            payload = {"update_id": counter, "message": message}
            if callback:
                payload = {
                    "update_id": counter,
                    "callback_query": {
                        "id": str(counter),
                        "from": user,
                        "chat_instance": "test",
                        "data": callback,
                        "message": message,
                    },
                }
            await app.process_update(Update.de_json(payload, app.bot))
            assert not failures, failures

        async def choose(uid, value):
            nonce = db.session(uid)[1]["nonce"]
            await dispatch(uid, callback=f"w:{nonce}:{value}")

        async with app:
            await service_bot.post_init(app)
            await dispatch(40, text="/start", chat_type="group")
            assert db.user(40) is None
            for uid in (1, 2):
                await dispatch(uid, text="/start")
                await dispatch(uid, callback="nav:register")
                await choose(uid, "agree")
                await dispatch(uid, text=f"Person {uid}")
                await dispatch(uid, text="17")
                assert db.session(uid)[0] == "age"
                await dispatch(uid, text="25")
                await choose(uid, "male")
                await choose(uid, "any")
                await dispatch(uid, text="Almaty")
                await dispatch(uid, text="I enjoy hiking.")
                await dispatch(uid, photo=True)
                assert db.session(uid)[0] == "preview"
                await choose(uid, "publish")
                assert db.user(uid)["active"]
                assert db.session(uid) is None
            await dispatch(1, text="/browse")
            token = db.conn.execute("SELECT token FROM views WHERE actor=1").fetchone()[0]
            await dispatch(1, callback=f"r:{token}:like")
            assert not db.is_match(1, 2)
            # Rebuild the service to simulate a restart; all state is in SQLite.
            service_bot.browse = type(service_bot.browse)(service_bot)
            await dispatch(2, text="/likes")
            token = db.conn.execute("SELECT token FROM views WHERE actor=2").fetchone()[0]
            await dispatch(2, callback=f"r:{token}:like")
            assert db.is_match(1, 2)
            sent = [p for e, p in transport.calls if e == "sendMessage"]
            notices = [p for p in sent if "It's a match!" in p["text"] or "Взаимная симпатия!" in p["text"]]
            assert len(notices) == 2
            assert {int(p["chat_id"]) for p in notices} == {1, 2}
            await dispatch(1, text="/matches")
            await dispatch(1, text="/edit")
            await dispatch(1, text="New name")
            await dispatch(1, text="/cancel")
            assert db.user(1)["name"] == "Person 1"
            await dispatch(1, text="/filters")
            await dispatch(1, text="22-30")
            await choose(1, "local")
            assert db.user(1)["local_only"]
            await dispatch(1, text="/pause")
            assert not db.user(1)["active"]
            await dispatch(1, text="/resume")
            assert db.user(1)["active"]
            await dispatch(1, text="/language")
            await dispatch(1, callback="lang:ru")
            assert db.user(1)["lang"] == "ru"
            await dispatch(1, text="/delete")
            old_nonce = db.session(1)[1]["nonce"]
            await dispatch(1, text="/cancel")
            await dispatch(1, callback=f"w:{old_nonce}:delete")
            assert db.user(1)["photo"]
            await dispatch(1, text="/delete")
            await choose(1, "delete")
            assert db.user(1) is None
            assert not db.matches(2)

    asyncio.run(scenario())
