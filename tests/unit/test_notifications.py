import asyncio
import threading
from uuid import uuid4

import pytest

from app.documents.notifications import JobNotifier


async def test_cancelled_notification_retains_single_publisher_slot(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def paused(self, document_id):
        calls.append(document_id)
        entered.set()
        release.wait(timeout=5)

    monkeypatch.setattr(JobNotifier, "_send", paused)
    notifier = JobNotifier("redis://127.0.0.1:1/0")
    first_id = uuid4()
    task = asyncio.create_task(notifier.notify(first_id))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(notifier.notify(uuid4()), timeout=0.2)
        assert calls == [first_id]
    finally:
        release.set()


async def test_slow_notification_has_overall_deadline(monkeypatch):
    release = threading.Event()

    def paused(self, document_id):
        release.wait(timeout=5)

    monkeypatch.setattr(JobNotifier, "_send", paused)
    notifier = JobNotifier("redis://127.0.0.1:1/0")
    try:
        async with asyncio.timeout(1.5):
            await notifier.notify(uuid4())
    finally:
        release.set()
