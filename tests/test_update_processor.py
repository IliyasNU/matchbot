import asyncio
from types import SimpleNamespace

import pytest

from update_processor import PerUserUpdateProcessor


def update(uid):
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid))


def test_same_person_stays_ordered_without_blocking_other_people():
    async def scenario():
        processor = PerUserUpdateProcessor(2)
        started, release, other_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        log = []

        async def first():
            log.append("first-start")
            started.set()
            await release.wait()
            log.append("first-end")

        async def later(index):
            log.append(index)

        async def other():
            other_done.set()

        async with processor:
            await processor.process_update(update(1), first())
            await started.wait()
            for index in range(40):
                await processor.process_update(update(1), later(index))
            await processor.process_update(update(2), other())
            await other_done.wait()
            assert log == ["first-start"]
            release.set()
            await processor.join()
            assert log == ["first-start", "first-end", *range(40)]
            assert not processor._queues and not processor._tasks

    asyncio.run(scenario())


def test_global_workers_and_admitted_handlers_are_bounded():
    async def scenario():
        processor = PerUserUpdateProcessor(2, pending_limit=4)
        started, release = asyncio.Event(), asyncio.Event()
        running, peak = 0, 0

        async def handler():
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            if running == 2:
                started.set()
            await release.wait()
            running -= 1

        for uid in range(4):
            await processor.process_update(update(uid), handler())
        await asyncio.wait_for(started.wait(), 3)
        fifth = asyncio.create_task(processor.process_update(update(5), handler()))
        await asyncio.sleep(0)
        assert not fifth.done() and len(processor._tasks) == 4 and peak == 2
        release.set()
        await asyncio.wait_for(fifth, 3)
        await asyncio.wait_for(processor.join(), 3)
        assert peak == 2 and not processor._queues

    asyncio.run(scenario())


def test_cancelled_admission_closes_handler_and_failure_does_not_stop_user_queue():
    async def scenario():
        processor = PerUserUpdateProcessor(1, pending_limit=1)
        started, release = asyncio.Event(), asyncio.Event()

        async def slow():
            started.set()
            await release.wait()

        async def never_run():
            raise AssertionError("Cancelled admission ran")

        await processor.process_update(update(1), slow())
        await started.wait()
        handler = never_run()
        waiting = asyncio.create_task(processor.process_update(update(2), handler))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert handler.cr_frame is None
        release.set()
        await processor.join()

        async def fail():
            raise ValueError("handler failure")

        await processor.process_update(update(1), fail())
        await processor.join()
        assert len(processor.errors) == 1 and isinstance(processor.errors[0], ValueError)
        assert not processor._queues

    asyncio.run(scenario())


def test_shutdown_drains_handlers_and_registry_does_not_grow_with_population():
    async def scenario():
        processor = PerUserUpdateProcessor(32)
        completed = 0

        async def complete():
            nonlocal completed
            completed += 1

        async with processor:
            for uid in range(10000):
                await processor.process_update(update(uid), complete())
        assert completed == 10000
        assert not processor._queues and not processor._tasks

    asyncio.run(scenario())


def test_real_application_stop_waits_for_admitted_handlers():
    from telegram.ext import Application, TypeHandler

    from tests.test_bot import FakeTelegram

    async def scenario():
        processor = PerUserUpdateProcessor(2, pending_limit=4)
        app = (
            Application.builder()
            .token("123456:simulation-only")
            .request(FakeTelegram())
            .concurrent_updates(processor)
            .update_queue(asyncio.Queue(maxsize=4))
            .build()
        )
        processor.bind(app)
        started, release, other_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        completed = []

        async def handler(item, context):
            if item.effective_user.id == 1:
                started.set()
                await release.wait()
            else:
                other_done.set()
            await context.bot.send_message(item.effective_user.id, "simulated")
            completed.append(item.effective_user.id)

        app.add_handler(TypeHandler(SimpleNamespace, handler))
        async with app:
            await app.start()
            await app.update_queue.put(update(1))
            await started.wait()
            await app.update_queue.put(update(2))
            await other_done.wait()
            stopping = asyncio.create_task(app.stop())
            await asyncio.sleep(0)
            assert not stopping.done()
            release.set()
            await stopping
            assert sorted(completed) == [1, 2]
            assert not processor._queues and not processor._tasks

    asyncio.run(scenario())
