"""Bounded admission with parallel people and FIFO actions within each person.

PTB sees a sequential dispatcher: it awaits admission before fetching another
update. Finite per-user runners are tracked by Application.create_task, so stop()
waits for admitted handlers before the Telegram HTTP client is closed.
"""

import asyncio
import inspect
from collections import deque

from telegram.ext import BaseUpdateProcessor


class PerUserUpdateProcessor(BaseUpdateProcessor):
    def __init__(self, worker_limit=32, pending_limit=2048):
        # A concurrent PTB fetcher creates an unbounded number of waiting tasks.
        # Keep its dispatcher sequential and manage bounded workers ourselves.
        super().__init__(1)
        if worker_limit < 1 or pending_limit < worker_limit:
            raise ValueError("Need 1 <= worker_limit <= pending_limit")
        self.worker_limit = worker_limit
        self.pending_limit = pending_limit
        self._workers = asyncio.Semaphore(worker_limit)
        self._admission = asyncio.Semaphore(pending_limit)
        self._queues = {}
        self._tasks = set()
        self._application = None
        self.errors = []

    def bind(self, application):
        self._application = application

    async def initialize(self):
        pass

    async def join(self):
        while self._tasks:
            tasks = tuple(self._tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            # gather() may complete synchronously for already-finished tasks;
            # don't spin while their scheduled discard callbacks wait to run.
            self._tasks.difference_update(tasks)

    async def shutdown(self):
        await self.join()

    async def do_process_update(self, update, coroutine):
        try:
            await self._admission.acquire()
        except BaseException:
            if inspect.iscoroutine(coroutine):
                coroutine.close()
            raise
        user = getattr(update, "effective_user", None)
        key = ("user", user.id) if user else ("other", id(update))
        queue = self._queues.get(key)
        if queue is None:
            queue = self._queues[key] = deque()
            queue.append((update, coroutine))
            runner = self._drain_user(key, queue)
            task = (
                self._application.create_task(runner) if self._application is not None else asyncio.create_task(runner)
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        else:
            queue.append((update, coroutine))

    async def _drain_user(self, key, queue):
        try:
            # A person's queued actions consume only one worker slot, so a burst
            # from one person cannot occupy all workers while waiting on a lock.
            async with self._workers:
                while queue:
                    update, coroutine = queue.popleft()
                    try:
                        await coroutine
                    except Exception as exc:
                        if self._application is not None:
                            await self._application.process_error(update=update, error=exc)
                        else:
                            self.errors.append(exc)
                    finally:
                        self._admission.release()
        finally:
            self._queues.pop(key, None)
            while queue:
                _, coroutine = queue.popleft()
                if inspect.iscoroutine(coroutine):
                    coroutine.close()
                self._admission.release()
