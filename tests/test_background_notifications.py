"""Discovery responses must not wait for unrelated members' notification delivery."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.error import NetworkError
from telegram.ext import Application

from bot import MatchBot
from config import Settings
from tests.conftest import member
from tests.test_bot import FakeTelegram, service


def setup_reaction(db):
    for uid in (1, 2, 3):
        member(db, uid)
    bot = service(db)
    bot.remove_buttons = AsyncMock()
    profile, token = db.present(1)
    return bot, profile["id"], token


def test_slow_notification_sender_cannot_delay_next_discovery_card(db):
    bot, target, token = setup_reaction(db)
    bot.schedule_notifications = Mock()

    async def scenario():
        next_card = asyncio.Event()
        release_sender = asyncio.Event()

        async def delayed_notifications():
            await release_sender.wait()

        async def show_card(*args, **kwargs):
            next_card.set()

        bot.notifications = AsyncMock(side_effect=delayed_notifications)
        bot.card = AsyncMock(side_effect=show_card)
        reaction = asyncio.create_task(bot.browse.react(1, token, "like", SimpleNamespace()))
        try:
            await asyncio.wait_for(next_card.wait(), timeout=3)
            await reaction
            bot.notifications.assert_not_awaited()
            bot.schedule_notifications.assert_called_once_with()
        finally:
            release_sender.set()
            await reaction

    asyncio.run(scenario())
    pending = db.pending_notifications()
    assert len(pending) == 1 and pending[0]["recipient"] == target


def test_notification_wakeup_happens_after_current_members_next_card(db):
    bot, _, token = setup_reaction(db)
    order = []

    async def show_card(*args, **kwargs):
        order.append("card")

    bot.card = AsyncMock(side_effect=show_card)
    bot.notifications = AsyncMock()
    bot.schedule_notifications = Mock(side_effect=lambda: order.append("wakeup"))
    asyncio.run(bot.browse.react(1, token, "like", SimpleNamespace()))
    assert order == ["card", "wakeup"]
    bot.notifications.assert_not_awaited()


@pytest.mark.parametrize("failed_ui", ["remove_buttons", "send", "card"])
def test_committed_like_still_wakes_sender_if_followup_ui_fails(db, failed_ui):
    bot, target, token = setup_reaction(db)
    bot.schedule_notifications = Mock()
    setattr(bot, failed_ui, AsyncMock(side_effect=NetworkError("offline")))
    with pytest.raises(NetworkError):
        asyncio.run(bot.browse.react(1, token, "like", SimpleNamespace()))
    bot.schedule_notifications.assert_called_once_with()
    pending = db.pending_notifications()
    assert len(pending) == 1 and pending[0]["recipient"] == target
    assert db.conn.execute("SELECT kind FROM reactions WHERE actor=1 AND target=?", (target,)).fetchone()[0] == "like"


@pytest.mark.parametrize("action", ["made-up", "report_unknown", "like"])
def test_invalid_or_stale_reaction_does_not_schedule_background_work(db, action):
    bot, _, _ = setup_reaction(db)
    bot.schedule_notifications = Mock()
    bot.notifications = AsyncMock()
    asyncio.run(bot.browse.react(1, "forged-token", action, SimpleNamespace()))
    bot.schedule_notifications.assert_not_called()
    bot.notifications.assert_not_awaited()
    assert not db.pending_notifications()


def test_opening_report_reasons_does_not_schedule_notification_delivery(db):
    bot, _, token = setup_reaction(db)
    bot.schedule_notifications = Mock()
    bot.notifications = AsyncMock()
    asyncio.run(bot.browse.react(1, token, "report", SimpleNamespace()))
    bot.schedule_notifications.assert_not_called()
    bot.notifications.assert_not_awaited()
    assert db.view(1, token)
    assert not db.pending_notifications()


def test_real_job_queue_coalesces_wakeups_and_shutdown_waits_for_sender(db):
    async def scenario():
        bot = MatchBot(db, Settings("123456:simulation-only", ":memory:"))
        app = Application.builder().token("123456:simulation-only").request(FakeTelegram()).build()
        bot.application = app
        started, release = asyncio.Event(), asyncio.Event()
        errors = []

        async def sender(context=None):
            started.set()
            await release.wait()

        async def on_error(update, context):
            errors.append(context.error)

        bot.notifications = AsyncMock(side_effect=sender)
        app.add_error_handler(on_error)
        stopping = None
        async with app:
            await app.start()
            try:
                for _ in range(100):
                    bot.schedule_notifications()
                await asyncio.wait_for(started.wait(), timeout=3)
                for _ in range(100):
                    bot.schedule_notifications()
                await asyncio.sleep(0)
                bot.notifications.assert_awaited_once()
                stopping = asyncio.create_task(app.stop())
                await asyncio.sleep(0)
                assert not stopping.done()
            finally:
                release.set()
                if stopping is not None:
                    await asyncio.wait_for(stopping, timeout=3)
                elif app.running:
                    await app.stop()
        assert not errors
        bot.notifications.assert_awaited_once()

    asyncio.run(scenario())


def test_failed_background_sender_can_be_scheduled_again(db):
    bot = service(db)
    jobs = []

    def run_once(callback, *args, **kwargs):
        jobs.append(callback)

    bot.application.job_queue = SimpleNamespace(run_once=run_once)
    bot.notifications = AsyncMock(side_effect=[NetworkError("offline"), None])

    async def scenario():
        bot.schedule_notifications()
        assert len(jobs) == 1
        with pytest.raises(NetworkError):
            await jobs.pop()(None)
        bot.schedule_notifications()
        assert len(jobs) == 1
        await jobs.pop()(None)

    asyncio.run(scenario())
    assert bot.notifications.await_count == 2
